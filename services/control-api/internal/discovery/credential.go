package discovery

// DDP-NODE-CREDENTIAL v1: the signing half. The verifying half lives in
// python/ddp_core/ddp_core/application/node_credentials.py. Both encode the
// same claims to the same bytes and are pinned to the frozen vector
// tests/fixtures/node-credential-v1.json (credential_crosslang_test.go here,
// test_node_credentials.py there) — two copies that are not mechanically
// compared drift silently, and this project has paid for that three times.
//
// Canonical form: JSON object, keys sorted, no whitespace. Every string is
// restricted to an ASCII subset that Go's encoder and Python's json.dumps
// emit byte-identically (Go escapes < > & and U+2028; those characters are
// simply not allowed), and times are integer epoch seconds.

import (
	"bytes"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"errors"
	"regexp"
	"strings"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/contracts"
)

const (
	CredentialSchema = "ddp-node-credential/1#Claims"
	CredentialAlg    = "Ed25519"
	// HeaderNodeCredential carries a single-use credential. Never logged or echoed.
	HeaderNodeCredential = "X-DDP-Node-Credential"
	// MaxCredentialLifetimeSeconds bounds expires_at - issued_at.
	MaxCredentialLifetimeSeconds = 120
	credentialDomain             = "ddp-node-credential/1\n"
	maxCredentialChars           = 4096
)

// ErrCredentialInvalid is the single structural/signature failure. Callers map
// it to the contract code credential_invalid; which check failed is not exposed.
var ErrCredentialInvalid = errors.New("credential_invalid")

var (
	credentialNode   = regexp.MustCompile(`^[a-z0-9][a-z0-9._-]{2,63}$`)
	credentialRef    = regexp.MustCompile(`^[A-Za-z0-9._:@+=-]{1,128}$`)
	credentialDigest = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)
	credentialPath   = regexp.MustCompile(`^/api/v1/federation/[A-Za-z0-9._:@+=/-]{1,256}$`)
	credentialJTI    = regexp.MustCompile(`^[A-Za-z0-9_-]{22,64}$`)
)

const credentialEpochMax = 253402300799

type CredentialActor struct {
	OrganizationID string `json:"organization_id"`
	Subject        string `json:"subject"`
	Kind           string `json:"kind"`
}

type CredentialConstraints struct {
	RootTaskID     string `json:"root_task_id"`
	StepID         string `json:"step_id,omitempty"`
	ScopeRef       string `json:"scope_ref,omitempty"`
	TaskSpecDigest string `json:"task_spec_digest,omitempty"`
}

type CredentialRequest struct {
	Method     string `json:"method"`
	Path       string `json:"path"`
	BodyDigest string `json:"body_digest"`
}

type CredentialClaims struct {
	Schema         string                `json:"schema"`
	Alg            string                `json:"alg"`
	IssuerNodeID   string                `json:"issuer_node_id"`
	AudienceNodeID string                `json:"audience_node_id"`
	Actor          CredentialActor       `json:"actor"`
	Operation      string                `json:"operation"`
	Constraints    CredentialConstraints `json:"constraints"`
	Request        CredentialRequest     `json:"request"`
	IssuedAt       int64                 `json:"issued_at"`
	ExpiresAt      int64                 `json:"expires_at"`
	JTI            string                `json:"jti"`
}

func credentialGetOperation(op contracts.NodeCredentialOperation) bool {
	switch op {
	case contracts.NodeCredentialOperationProbeRead, contracts.NodeCredentialOperationExecutionRead,
		contracts.NodeCredentialOperationEvidenceSetRead, contracts.NodeCredentialOperationCatalogRead:
		return true
	}
	return false
}

// Validate applies the contract Claims rules plus the three JSON Schema cannot
// express (issuer != audience, lifetime bound, issued before expiry). It is the
// same rule set as ddp_core.application.node_credentials.validate_claims.
func (c CredentialClaims) Validate() error {
	if c.Schema != CredentialSchema || c.Alg != CredentialAlg {
		return ErrCredentialInvalid
	}
	if !credentialNode.MatchString(c.IssuerNodeID) || !credentialNode.MatchString(c.AudienceNodeID) || c.IssuerNodeID == c.AudienceNodeID {
		return ErrCredentialInvalid
	}
	if !credentialRef.MatchString(c.Actor.OrganizationID) || !credentialRef.MatchString(c.Actor.Subject) || !contracts.ActorKind(c.Actor.Kind).Valid() {
		return ErrCredentialInvalid
	}
	op := contracts.NodeCredentialOperation(c.Operation)
	if !op.Valid() {
		return ErrCredentialInvalid
	}
	method := "POST"
	if credentialGetOperation(op) {
		method = "GET"
	}
	if c.Request.Method != method || !credentialPath.MatchString(c.Request.Path) || !credentialDigest.MatchString(c.Request.BodyDigest) {
		return ErrCredentialInvalid
	}
	k := c.Constraints
	if !credentialRef.MatchString(k.RootTaskID) {
		return ErrCredentialInvalid
	}
	for _, optional := range []string{k.StepID, k.ScopeRef} {
		if optional != "" && !credentialRef.MatchString(optional) {
			return ErrCredentialInvalid
		}
	}
	if k.TaskSpecDigest != "" && !credentialDigest.MatchString(k.TaskSpecDigest) {
		return ErrCredentialInvalid
	}
	if op == contracts.NodeCredentialOperationAdmissionCreate && k.StepID == "" {
		return ErrCredentialInvalid
	}
	if (op == contracts.NodeCredentialOperationProbeCreate || op == contracts.NodeCredentialOperationProbeRead) && k.TaskSpecDigest == "" {
		return ErrCredentialInvalid
	}
	if c.IssuedAt < 1 || c.ExpiresAt > credentialEpochMax || c.ExpiresAt-c.IssuedAt < 1 || c.ExpiresAt-c.IssuedAt > MaxCredentialLifetimeSeconds {
		return ErrCredentialInvalid
	}
	if !credentialJTI.MatchString(c.JTI) {
		return ErrCredentialInvalid
	}
	return nil
}

func (c CredentialClaims) canonicalValue() map[string]any {
	constraints := map[string]any{"root_task_id": c.Constraints.RootTaskID}
	if c.Constraints.StepID != "" {
		constraints["step_id"] = c.Constraints.StepID
	}
	if c.Constraints.ScopeRef != "" {
		constraints["scope_ref"] = c.Constraints.ScopeRef
	}
	if c.Constraints.TaskSpecDigest != "" {
		constraints["task_spec_digest"] = c.Constraints.TaskSpecDigest
	}
	return map[string]any{
		"schema":           c.Schema,
		"alg":              c.Alg,
		"issuer_node_id":   c.IssuerNodeID,
		"audience_node_id": c.AudienceNodeID,
		"actor": map[string]any{
			"organization_id": c.Actor.OrganizationID,
			"subject":         c.Actor.Subject,
			"kind":            c.Actor.Kind,
		},
		"operation":   c.Operation,
		"constraints": constraints,
		"request": map[string]any{
			"method":      c.Request.Method,
			"path":        c.Request.Path,
			"body_digest": c.Request.BodyDigest,
		},
		"issued_at":  c.IssuedAt,
		"expires_at": c.ExpiresAt,
		"jti":        c.JTI,
	}
}

// CanonicalCredential returns the exact bytes a signature covers. Invalid claims
// have no canonical form. encoding/json sorts map keys, which is the ordering rule.
func CanonicalCredential(c CredentialClaims) ([]byte, error) {
	if err := c.Validate(); err != nil {
		return nil, err
	}
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(c.canonicalValue()); err != nil {
		return nil, ErrCredentialInvalid
	}
	return bytes.TrimSuffix(buf.Bytes(), []byte("\n")), nil
}

// NewCredentialJTI returns 128 random bits, unpadded base64url.
func NewCredentialJTI() (string, error) {
	var raw [16]byte
	if _, err := rand.Read(raw[:]); err != nil {
		return "", errors.New("cannot generate credential id")
	}
	return base64.RawURLEncoding.EncodeToString(raw[:]), nil
}

// SignCredential signs claims with the node's private key. The issuer must be
// this identity: a node never signs on behalf of another node.
func (i *Identity) SignCredential(c CredentialClaims) (string, error) {
	if c.IssuerNodeID != i.NodeID() {
		return "", ErrCredentialInvalid
	}
	payload, err := CanonicalCredential(c)
	if err != nil {
		return "", err
	}
	signature := ed25519.Sign(i.private, append([]byte(credentialDomain), payload...))
	return base64.RawURLEncoding.EncodeToString(payload) + "." + base64.RawURLEncoding.EncodeToString(signature), nil
}

func strictRawURL(text string) ([]byte, bool) {
	if text == "" {
		return nil, false
	}
	raw, err := base64.RawURLEncoding.Strict().DecodeString(text)
	if err != nil || base64.RawURLEncoding.EncodeToString(raw) != text {
		return nil, false
	}
	return raw, true
}

// VerifyCredential decodes a credential strictly and checks its signature
// against a standard-base64 Ed25519 public key. Control never receives
// credentials in production (corpus verifies); this exists so Go tests can
// refuse the same frozen invalid vectors the Python verifier refuses, and so a
// future Go receiver has one implementation to call rather than a second copy.
func VerifyCredential(token, publicKey string) (CredentialClaims, error) {
	var none CredentialClaims
	if token == "" || len(token) > maxCredentialChars {
		return none, ErrCredentialInvalid
	}
	parts := strings.Split(token, ".")
	if len(parts) != 2 {
		return none, ErrCredentialInvalid
	}
	payload, ok := strictRawURL(parts[0])
	if !ok {
		return none, ErrCredentialInvalid
	}
	signature, ok := strictRawURL(parts[1])
	if !ok || len(signature) != ed25519.SignatureSize {
		return none, ErrCredentialInvalid
	}
	var claims CredentialClaims
	decoder := json.NewDecoder(bytes.NewReader(payload))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&claims); err != nil {
		return none, ErrCredentialInvalid
	}
	// Re-encoding must reproduce the received bytes exactly. This also rejects
	// duplicate keys (encoding/json keeps the last one silently), case-folded
	// keys (it matches field names case-insensitively) and any whitespace.
	canonical, err := CanonicalCredential(claims)
	if err != nil || !bytes.Equal(canonical, payload) {
		return none, ErrCredentialInvalid
	}
	public, err := base64.StdEncoding.Strict().DecodeString(publicKey)
	if err != nil || len(public) != ed25519.PublicKeySize || base64.StdEncoding.EncodeToString(public) != publicKey {
		return none, ErrCredentialInvalid
	}
	if !ed25519.Verify(ed25519.PublicKey(public), append([]byte(credentialDomain), payload...), signature) {
		return none, ErrCredentialInvalid
	}
	return claims, nil
}
