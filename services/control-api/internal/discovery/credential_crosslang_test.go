package discovery

import (
	"crypto/ed25519"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"runtime"
	"testing"
)

// crossLangVector mirrors tests/fixtures/node-credential-v1.json. The Python
// verifier (python/ddp_core/tests/test_node_credentials.py) reads the same file.
type crossLangVector struct {
	IssuerSeedHex    string           `json:"issuer_seed_hex"`
	IssuerPublicKey  string           `json:"issuer_public_key"`
	IssuerNodeID     string           `json:"issuer_node_id"`
	AudienceNodeID   string           `json:"audience_node_id"`
	AudiencePublic   string           `json:"audience_public_key"`
	Claims           CredentialClaims `json:"claims"`
	CanonicalPayload string           `json:"canonical_payload"`
	SignatureB64URL  string           `json:"signature_b64url"`
	Credential       string           `json:"credential"`
	Invalid          []struct {
		Name       string `json:"name"`
		Credential string `json:"credential"`
		Code       string `json:"code"`
	} `json:"invalid"`
}

func loadCrossLangVector(t *testing.T) crossLangVector {
	t.Helper()
	_, here, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("cannot locate test file")
	}
	// services/control-api/internal/discovery -> repository root
	path := filepath.Join(filepath.Dir(here), "..", "..", "..", "..", "tests", "fixtures", "node-credential-v1.json")
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("frozen cross-language vector missing: %v", err)
	}
	var v crossLangVector
	if err := json.Unmarshal(raw, &v); err != nil {
		t.Fatal(err)
	}
	if len(v.Invalid) == 0 || v.Credential == "" {
		t.Fatal("cross-language vector is empty; the comparison below would prove nothing")
	}
	return v
}

func identityFromSeedHex(t *testing.T, seedHex string) *Identity {
	t.Helper()
	seed, err := hex.DecodeString(seedHex)
	if err != nil || len(seed) != ed25519.SeedSize {
		t.Fatal("bad test seed")
	}
	private := ed25519.NewKeyFromSeed(seed)
	return &Identity{private: private, public: private.Public().(ed25519.PublicKey)}
}

// TestCredentialCrossLanguageVector re-signs the frozen claims and must
// reproduce the Python-produced bytes, signature and token exactly.
func TestCredentialCrossLanguageVector(t *testing.T) {
	v := loadCrossLangVector(t)
	issuer := identityFromSeedHex(t, v.IssuerSeedHex)
	if issuer.NodeID() != v.IssuerNodeID || issuer.PublicKey() != v.IssuerPublicKey {
		t.Fatalf("node id derivation drifted: %s", issuer.NodeID())
	}
	if id, err := NodeIDForPublicKey(v.AudiencePublic); err != nil || id != v.AudienceNodeID {
		t.Fatalf("audience id derivation drifted: %s %v", id, err)
	}
	canonical, err := CanonicalCredential(v.Claims)
	if err != nil {
		t.Fatal(err)
	}
	if string(canonical) != v.CanonicalPayload {
		t.Fatalf("canonical encoding drifted:\n go: %s\n py: %s", canonical, v.CanonicalPayload)
	}
	token, err := issuer.SignCredential(v.Claims)
	if err != nil {
		t.Fatal(err)
	}
	if token != v.Credential {
		t.Fatalf("credential drifted:\n go: %s\n py: %s", token, v.Credential)
	}
	claims, err := VerifyCredential(v.Credential, v.IssuerPublicKey)
	if err != nil || claims != v.Claims {
		t.Fatalf("frozen credential does not verify in Go: %v", err)
	}
	for _, bad := range v.Invalid {
		if bad.Code != "credential_invalid" {
			t.Fatalf("%s: unexpected code in vector %s", bad.Name, bad.Code)
		}
		if _, err := VerifyCredential(bad.Credential, v.IssuerPublicKey); err == nil {
			t.Fatalf("%s: Go accepted a credential the contract refuses", bad.Name)
		}
	}
}

func TestCredentialSigningRefusesForeignIssuerAndInvalidClaims(t *testing.T) {
	v := loadCrossLangVector(t)
	issuer := identityFromSeedHex(t, v.IssuerSeedHex)
	foreign := v.Claims
	foreign.IssuerNodeID = v.AudienceNodeID
	foreign.AudienceNodeID = v.IssuerNodeID
	if _, err := issuer.SignCredential(foreign); err == nil {
		t.Fatal("a node signed a credential naming another node as issuer")
	}
	cases := map[string]func(*CredentialClaims){
		"lifetime":       func(c *CredentialClaims) { c.ExpiresAt = c.IssuedAt + MaxCredentialLifetimeSeconds + 1 },
		"html_subject":   func(c *CredentialClaims) { c.Actor.Subject = "a<b" },
		"unknown_op":     func(c *CredentialClaims) { c.Operation = "admin" },
		"unknown_kind":   func(c *CredentialClaims) { c.Actor.Kind = "peer" },
		"missing_step":   func(c *CredentialClaims) { c.Constraints.StepID = "" },
		"get_for_write":  func(c *CredentialClaims) { c.Request.Method = "GET" },
		"self_audience":  func(c *CredentialClaims) { c.AudienceNodeID = c.IssuerNodeID },
		"internal_path":  func(c *CredentialClaims) { c.Request.Path = "/internal/capabilities" },
		"short_jti":      func(c *CredentialClaims) { c.JTI = "x" },
		"zero_lifetime":  func(c *CredentialClaims) { c.ExpiresAt = c.IssuedAt },
		"non_ascii_root": func(c *CredentialClaims) { c.Constraints.RootTaskID = "根" },
	}
	for name, mutate := range cases {
		c := v.Claims
		mutate(&c)
		if _, err := issuer.SignCredential(c); err == nil {
			t.Fatalf("%s: signed invalid claims", name)
		}
	}
	// Another key's signature over the frozen claims is not the issuer's.
	other := identityFromSeedHex(t, "0909090909090909090909090909090909090909090909090909090909090909")
	forged := v.Claims
	forged.IssuerNodeID = other.NodeID()
	token, err := other.SignCredential(forged)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := VerifyCredential(token, v.IssuerPublicKey); err == nil {
		t.Fatal("verified a credential signed by a different key")
	}
}
