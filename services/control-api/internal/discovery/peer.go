package discovery

import (
	"context"
	"crypto/tls"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"time"
)

// Peer-facing read APIs are authenticated by a node-level shared credential.
// The corpus side has the same shape (`ddp_corpus/routers/federation.py`):
// fail closed on an unconfigured token, constant-time compare, no actor context.
const (
	HeaderPeerToken  = "X-DDP-Peer-Token"
	HeaderPeerTarget = "X-DDP-Target-Node"

	peerMaxResponseBytes = 8 * 1024 * 1024
	DefaultPeerTimeout   = 10 * time.Second
)

var peerNodePattern = regexp.MustCompile(`^[a-z0-9][a-z0-9._-]{2,63}$`)

// PeerConfig is one administrator-registered outbound node. Credential fields
// never appear in logs, errors or responses.
type PeerConfig struct {
	NodeID       string
	Endpoint     string
	ServiceToken string
	PeerToken    string
}

func validatePeerEndpoint(nodeID, endpoint string, allowLoopback bool) (string, error) {
	if endpoint == "" || len(endpoint) > 512 {
		return "", fmt.Errorf("peer %s: endpoint missing or too long", nodeID)
	}
	u, err := url.Parse(endpoint)
	if err != nil {
		return "", fmt.Errorf("peer %s: endpoint is not a URL", nodeID)
	}
	loopback := allowLoopback && (u.Hostname() == "127.0.0.1" || u.Hostname() == "::1")
	if (u.Scheme != "https" && !loopback) || u.Hostname() == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" {
		return "", fmt.Errorf("peer %s: endpoint must be https without userinfo/query/fragment (http only for literal loopback with FEDERATION_ALLOW_LOOPBACK)", nodeID)
	}
	return strings.TrimRight(endpoint, "/"), nil
}

// ParsePeers validates the administrator directory. Empty input is an empty
// directory; any malformed entry is a configuration error, never a silent skip.
func ParsePeers(raw string, allowLoopback bool) (map[string]PeerConfig, error) {
	if strings.TrimSpace(raw) == "" {
		return map[string]PeerConfig{}, nil
	}
	var value map[string]struct {
		Endpoint     string `json:"endpoint"`
		ServiceToken string `json:"service_token"`
		PeerToken    string `json:"peer_token"`
	}
	decoder := json.NewDecoder(strings.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&value); err != nil {
		return nil, fmt.Errorf("FEDERATION_PEERS is not a valid JSON object: %w", err)
	}
	peers := make(map[string]PeerConfig, len(value))
	for nodeID, item := range value {
		if !peerNodePattern.MatchString(nodeID) {
			return nil, fmt.Errorf("FEDERATION_PEERS has an invalid node id")
		}
		endpoint, err := validatePeerEndpoint(nodeID, item.Endpoint, allowLoopback)
		if err != nil {
			return nil, err
		}
		if item.ServiceToken == "" || item.PeerToken == "" {
			return nil, fmt.Errorf("peer %s: service_token and peer_token are required", nodeID)
		}
		peers[nodeID] = PeerConfig{NodeID: nodeID, Endpoint: endpoint, ServiceToken: item.ServiceToken, PeerToken: item.PeerToken}
	}
	return peers, nil
}

// PeerDirectory holds the parsed registry plus the transport used for tests.
// A nil directory means "no outbound expansion": no remote node is contacted.
type PeerDirectory struct {
	peers map[string]PeerConfig
	// client is built once in NewPeerDirectory; the zero value is never used.
	client *http.Client
}

func NewPeerDirectory(peers map[string]PeerConfig, transport http.RoundTripper, timeout time.Duration) *PeerDirectory {
	if timeout <= 0 {
		timeout = DefaultPeerTimeout
	}
	if transport == nil {
		transport = &http.Transport{
			// 不读环境代理变量：内网调用被塞进代理的表现是卡住而不是报错（铁律 8）。
			Proxy: nil,
			DialContext: (&net.Dialer{
				Timeout:   5 * time.Second,
				KeepAlive: 30 * time.Second,
			}).DialContext,
			TLSClientConfig:       &tls.Config{MinVersion: tls.VersionTLS12},
			MaxIdleConns:          32,
			MaxIdleConnsPerHost:   8,
			IdleConnTimeout:       60 * time.Second,
			ExpectContinueTimeout: time.Second,
			ForceAttemptHTTP2:     true,
		}
	}
	return &PeerDirectory{
		peers: peers,
		// The client is built once so concurrent scope creations share it without
		// a lazy-initialization race.
		client: &http.Client{
			Transport: transport,
			Timeout:   timeout,
			// 重定向一律不跟：换个 host 继续请求等于把同伴凭据交给未登记节点。
			CheckRedirect: func(_ *http.Request, _ []*http.Request) error { return http.ErrUseLastResponse },
		},
	}
}

func (d *PeerDirectory) Configured(nodeID string) (PeerConfig, bool) {
	if d == nil {
		return PeerConfig{}, false
	}
	cfg, ok := d.peers[nodeID]
	return cfg, ok
}

func (d *PeerDirectory) Len() int {
	if d == nil {
		return 0
	}
	return len(d.peers)
}

// peerGet performs one authenticated read against a registered endpoint.
// The returned reason is "" on a 200, the honest failure reason otherwise
// (timeout/denied/unknown). Redirects are reported as unknown and never followed.
func (d *PeerDirectory) peerGet(ctx context.Context, cfg PeerConfig, path string, query url.Values) ([]byte, int, string) {
	target := cfg.Endpoint + path
	if len(query) > 0 {
		target += "?" + query.Encode()
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, target, nil)
	if err != nil {
		return nil, 0, "unknown"
	}
	req.Header.Set("Authorization", "Bearer "+cfg.ServiceToken)
	req.Header.Set(HeaderPeerToken, cfg.PeerToken)
	req.Header.Set(HeaderPeerTarget, cfg.NodeID)
	req.Header.Set("Accept", "application/json")
	resp, err := d.client.Do(req)
	if err != nil {
		if ctx.Err() != nil || isTimeout(err) {
			return nil, 0, "timeout"
		}
		return nil, 0, "unknown"
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, peerMaxResponseBytes+1))
	if err != nil || len(body) > peerMaxResponseBytes {
		return nil, resp.StatusCode, "unknown"
	}
	switch {
	case resp.StatusCode == http.StatusOK:
		return body, resp.StatusCode, ""
	case resp.StatusCode == http.StatusUnauthorized || resp.StatusCode == http.StatusForbidden:
		return body, resp.StatusCode, "denied"
	default:
		return body, resp.StatusCode, "unknown"
	}
}

func isTimeout(err error) bool {
	var netErr net.Error
	return errors.As(err, &netErr) && netErr.Timeout()
}

// MembersPage fetches one stable page of a peer's approved direct directory.
func (d *PeerDirectory) MembersPage(ctx context.Context, cfg PeerConfig, snapshotID, cursor string, limit int) (*PeerMemberPage, string) {
	query := url.Values{}
	if snapshotID != "" {
		query.Set("snapshot_id", snapshotID)
	}
	if cursor != "" {
		query.Set("cursor", cursor)
	}
	if limit > 0 {
		query.Set("limit", fmt.Sprint(limit))
	}
	body, _, reason := d.peerGet(ctx, cfg, "/api/v1/federation/members", query)
	if reason != "" {
		return nil, reason
	}
	var page PeerMemberPage
	if json.Unmarshal(body, &page) != nil || !page.valid(cfg.NodeID) {
		return nil, "unknown"
	}
	return &page, ""
}

// CollectionsPage fetches one stable page of a peer's published collections.
func (d *PeerDirectory) CollectionsPage(ctx context.Context, cfg PeerConfig, snapshotID, cursor string, limit int) (*PeerCatalogPage, string) {
	query := url.Values{}
	if snapshotID != "" {
		query.Set("snapshot_id", snapshotID)
	}
	if cursor != "" {
		query.Set("cursor", cursor)
	}
	if limit > 0 {
		query.Set("limit", fmt.Sprint(limit))
	}
	body, status, reason := d.peerGet(ctx, cfg, "/api/v1/federation/collections", query)
	if reason != "" {
		// A withdrawn/expired peer snapshot reports 410; the collector keeps its
		// already observed targets and must not fabricate completion either way.
		if status == http.StatusGone || status == http.StatusConflict {
			return nil, "unknown"
		}
		return nil, reason
	}
	var page PeerCatalogPage
	if json.Unmarshal(body, &page) != nil || !page.valid(cfg.NodeID) {
		return nil, "unknown"
	}
	return &page, ""
}
