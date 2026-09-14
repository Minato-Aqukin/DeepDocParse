// Package discovery defines the control-owned, approved direct-member directory.
// It never connects to registered endpoints or turns node trust into resource permission.
package discovery

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"net/url"
	"slices"
	"strings"
	"time"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/contracts"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/identity"
)

const (
	MemberPending         = string(contracts.NodeMembershipStatePending)
	MemberApproved        = string(contracts.NodeMembershipStateApproved)
	MemberRevoked         = string(contracts.NodeMembershipStateRevoked)
	ExpansionNotRequested = string(contracts.MemberExpansionStateNotRequested)
	ExpansionUnexpanded   = string(contracts.MemberExpansionStateUnexpandedSubtree)
	ExpansionRevoked      = string(contracts.MemberExpansionStateSourceRevoked)
	HealthUnknown         = string(contracts.CapabilityReadinessUnknown)
)

type Endpoint struct {
	Purpose string `json:"purpose"`
	URL     string `json:"url"`
}
type DiscoveryCapabilities struct {
	EnumerateMembers bool `json:"enumerate_members"`
	CatalogEvents    bool `json:"catalog_events"`
}
type NodeDescriptor struct {
	Schema                string                `json:"schema"`
	NodeID                string                `json:"node_id"`
	ProtocolVersions      []string              `json:"protocol_versions"`
	ControlledEndpoints   []Endpoint            `json:"controlled_endpoints"`
	AuthMethods           []string              `json:"auth_methods"`
	DiscoveryCapabilities DiscoveryCapabilities `json:"discovery_capabilities"`
	Revision              int64                 `json:"revision"`
	ValidUntil            time.Time             `json:"valid_until"`
	PublisherSignature    string                `json:"publisher_signature,omitempty"`
}

func (d NodeDescriptor) Validate(publicKey, localID string, now time.Time) error {
	id, err := NodeIDForPublicKey(publicKey)
	if err != nil || d.NodeID != id || d.NodeID == localID {
		return errors.New("node identity mismatch or self-registration")
	}
	if d.Schema != "ddp-discovery/1#NodeDescriptor" || d.Revision < 1 || !d.ValidUntil.After(now) {
		return errors.New("invalid or expired descriptor")
	}
	if !slices.Contains(d.ProtocolVersions, "ddp-discovery/1") {
		return errors.New("unsupported discovery protocol")
	}
	if len(d.AuthMethods) == 0 || len(d.ControlledEndpoints) == 0 || len(d.ControlledEndpoints) > 8 {
		return errors.New("auth methods and controlled endpoints required")
	}
	for _, e := range d.ControlledEndpoints {
		u, err := url.Parse(e.URL)
		if err != nil || (u.Scheme != "https" && u.Scheme != "http") || u.Hostname() == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" || (e.Purpose != "federation" && e.Purpose != "data_channel") {
			return errors.New("invalid controlled endpoint")
		}
	}
	return nil
}

type RouteRecord struct {
	Schema        string    `json:"schema"`
	OriginNodeID  string    `json:"origin_node_id"`
	NextHopNodeID string    `json:"next_hop_node_id"`
	Path          []string  `json:"path"`
	ObservedBy    string    `json:"observed_by"`
	ObservedAt    time.Time `json:"observed_at"`
	ValidUntil    time.Time `json:"valid_until"`
}
type Member struct {
	NodeID              string          `json:"node_id"`
	State               string          `json:"state"`
	Revision            int64           `json:"revision"`
	Descriptor          *NodeDescriptor `json:"descriptor,omitempty"`
	Route               *RouteRecord    `json:"route,omitempty"`
	Configured          bool            `json:"configured"`
	Health              string          `json:"health"`
	AcceptingAdmissions bool            `json:"accepting_admissions"`
	ExpansionState      string          `json:"expansion_state"`
}
type Registration struct {
	Descriptor      NodeDescriptor `json:"descriptor"`
	PublicKey       string         `json:"public_key"`
	VisibleToOrg    bool           `json:"visible_to_org"`
	AllowedSubjects []string       `json:"allowed_subjects"`
}

func DirectMember(d NodeDescriptor, state string, revision int64, localID string, now time.Time) Member {
	expansion := ExpansionUnexpanded
	if d.DiscoveryCapabilities.EnumerateMembers {
		expansion = ExpansionNotRequested
	}
	return Member{NodeID: d.NodeID, State: state, Revision: revision, Descriptor: &d,
		Route:      &RouteRecord{Schema: "ddp-discovery/1#RouteRecord", OriginNodeID: d.NodeID, NextHopNodeID: d.NodeID, Path: []string{localID, d.NodeID}, ObservedBy: localID, ObservedAt: now, ValidUntil: d.ValidUntil},
		Configured: state == MemberApproved, Health: HealthUnknown, AcceptingAdmissions: false, ExpansionState: expansion}
}

type Snapshot struct {
	ID               string    `json:"snapshot_id"`
	AuthorityNodeID  string    `json:"authority_node_id"`
	CallerScopeHash  string    `json:"caller_scope_hash"`
	RegistryRevision int64     `json:"registry_revision"`
	CreatedAt        time.Time `json:"created_at"`
	ExpiresAt        time.Time `json:"expires_at"`
	FirstCursor      string    `json:"first_cursor"`
	TerminalCursor   string    `json:"terminal_cursor"`
}
type Page struct {
	Snapshot
	Members    []Member `json:"members"`
	NextCursor *string  `json:"next_cursor"`
	Complete   bool     `json:"complete"`
}

// ScopeHash binds a snapshot to current, freshly authenticated membership and key scopes.
func ScopeHash(a *identity.Actor) string {
	scopes := make([]string, 0, len(a.Scopes))
	for _, s := range a.Scopes {
		scopes = append(scopes, string(s))
	}
	slices.Sort(scopes)
	b, _ := json.Marshal([]any{a.OrganizationID, a.UserID, a.ID, a.Kind, a.Role, scopes})
	sum := sha256.Sum256(b)
	return "sha256:" + hex.EncodeToString(sum[:])
}
func ValidBaseURL(raw string) bool {
	for _, c := range raw {
		if c > 127 || c < 33 {
			return false
		}
	}
	u, e := url.Parse(raw)
	return e == nil && (u.Scheme == "https" || u.Scheme == "http") && u.Hostname() != "" && u.User == nil && u.RawQuery == "" && u.Fragment == "" && !strings.ContainsAny(raw, "\r\n")
}

// An omitted enumeration declaration is not the same contract value as explicit
// false. Preserve that distinction at the administrative configuration boundary.
func (d *NodeDescriptor) UnmarshalJSON(body []byte) error {
	type descriptor NodeDescriptor
	var decoded descriptor
	decoder := json.NewDecoder(strings.NewReader(string(body)))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&decoded); err != nil {
		return err
	}
	var raw map[string]json.RawMessage
	if err := json.Unmarshal(body, &raw); err != nil {
		return err
	}
	var capability map[string]json.RawMessage
	if err := json.Unmarshal(raw["discovery_capabilities"], &capability); err != nil {
		return errors.New("discovery capabilities required")
	}
	flag, exists := capability["enumerate_members"]
	if !exists || strings.TrimSpace(string(flag)) == "null" {
		return errors.New("enumerate_members must be explicitly declared")
	}
	*d = NodeDescriptor(decoded)
	return nil
}
