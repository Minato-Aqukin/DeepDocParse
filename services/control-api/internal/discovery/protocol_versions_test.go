package discovery

import (
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"testing"
	"time"
)

// registeredDescriptor builds a descriptor that passes every non-protocol check,
// so a failure below can only come from the protocol version under test.
func registeredDescriptor(t *testing.T) (NodeDescriptor, string) {
	t.Helper()
	public, _, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	encoded := base64.StdEncoding.EncodeToString(public)
	nodeID, err := NodeIDForPublicKey(encoded)
	if err != nil {
		t.Fatal(err)
	}
	return NodeDescriptor{
		Schema:              "ddp-discovery/1#NodeDescriptor",
		NodeID:              nodeID,
		ProtocolVersions:    []string{"ddp-discovery/1"},
		ControlledEndpoints: []Endpoint{{Purpose: "federation", URL: "https://peer.example/api/v1/federation"}},
		AuthMethods:         []string{"session"},
		Revision:            1,
		ValidUntil:          time.Now().Add(time.Hour),
	}, encoded
}

func TestNodeDescriptorRejectsUnknownDiscoveryProtocolVersion(t *testing.T) {
	local := "node-local"
	descriptor, publicKey := registeredDescriptor(t)
	if err := descriptor.Validate(publicKey, local, time.Now()); err != nil {
		t.Fatalf("baseline descriptor must validate: %v", err)
	}
	// A peer that only speaks an unknown version cannot be executed against.
	descriptor.ProtocolVersions = []string{"ddp-discovery/99"}
	if err := descriptor.Validate(publicKey, local, time.Now()); err == nil {
		t.Fatal("unknown discovery protocol version was accepted")
	}
	descriptor.ProtocolVersions = nil
	if err := descriptor.Validate(publicKey, local, time.Now()); err == nil {
		t.Fatal("empty protocol version list was accepted")
	}
	// Advertising a known version alongside unknown ones is forward compatible:
	// the caller executes the version it knows and ignores the rest.
	descriptor.ProtocolVersions = []string{"ddp-discovery/99", "ddp-discovery/1", "ddp-client/1"}
	if err := descriptor.Validate(publicKey, local, time.Now()); err != nil {
		t.Fatalf("known version alongside unknown ones must stay usable: %v", err)
	}
}

func TestNodeDescriptorRegistrationRejectsUnknownFieldsInsteadOfIgnoringThem(t *testing.T) {
	// An old control must fail closed on a newer registration shape rather than
	// silently dropping the field and registering a node it cannot honour.
	descriptor, publicKey := registeredDescriptor(t)
	raw, err := json.Marshal(descriptor)
	if err != nil {
		t.Fatal(err)
	}
	var body map[string]any
	if err := json.Unmarshal(raw, &body); err != nil {
		t.Fatal(err)
	}
	body["required_client_capabilities"] = []string{"future-thing"}
	encoded, err := json.Marshal(body)
	if err != nil {
		t.Fatal(err)
	}
	var decoded NodeDescriptor
	if err := decoded.UnmarshalJSON(encoded); err == nil {
		t.Fatal("unknown required field was silently ignored")
	}
	// The same decode succeeds for the exact known shape, proving the field --
	// not the whole test -- is what the rejection comes from.
	if err := decoded.UnmarshalJSON(raw); err != nil {
		t.Fatalf("known shape must decode: %v", err)
	}
	if decoded.NodeID != descriptor.NodeID {
		t.Fatal("decoded descriptor identity changed")
	}
	_ = publicKey
}
