package discovery

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"errors"
	"strings"
	"time"
)

// NodeProof demonstrates possession of the key for this configured authority.
// Host/Forwarded headers and caller-controlled endpoint values never enter it.
type NodeProof struct {
	Schema    string `json:"schema"`
	Nonce     string `json:"nonce"`
	NodeID    string `json:"node_id"`
	Endpoint  string `json:"endpoint"`
	IssuedAt  string `json:"issued_at"`
	ExpiresAt string `json:"expires_at"`
	Signature string `json:"signature"`
}

func ValidChallenge(nonce string) bool {
	if len(nonce) < 32 || len(nonce) > 64 {
		return false
	}
	raw, err := base64.RawURLEncoding.Strict().DecodeString(nonce)
	return err == nil && base64.RawURLEncoding.EncodeToString(raw) == nonce
}
func (i *Identity) Proof(nonce, endpoint string, now time.Time) (NodeProof, error) {
	if !ValidChallenge(nonce) || !ValidBaseURL(endpoint) {
		return NodeProof{}, errors.New("invalid node proof request")
	}
	// PublicBaseURL is an ASCII HTTP(S) URL so Go's encoding equals JSON.stringify.
	p := NodeProof{Schema: "ddp-node-proof/1", Nonce: nonce, NodeID: i.NodeID(), Endpoint: strings.TrimRight(endpoint, "/"), IssuedAt: now.UTC().Truncate(time.Second).Format(time.RFC3339), ExpiresAt: now.UTC().Truncate(time.Second).Add(time.Minute).Format(time.RFC3339)}
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode([]string{p.Schema, p.Nonce, p.NodeID, p.Endpoint, p.IssuedAt, p.ExpiresAt}); err != nil {
		return NodeProof{}, err
	}
	payload := bytes.TrimSuffix(buf.Bytes(), []byte("\n"))
	signed, err := base64.StdEncoding.DecodeString(i.Sign(payload))
	if err != nil {
		return NodeProof{}, err
	}
	p.Signature = base64.RawURLEncoding.EncodeToString(signed)
	return p, nil
}
