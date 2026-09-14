package api

import (
	"net/http/httptest"
	"slices"
	"testing"
	"time"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/discovery"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/httpx"
)

// The public node handshake is how a client learns which protocol versions the
// authority speaks. It must advertise the frozen set, not the caller's guess.
func TestFederationNodeHandshakeAdvertisesSupportedProtocolVersions(t *testing.T) {
	s := proofServer(t)
	r := httptest.NewRequest("GET", "/api/v1/federation/node", nil)
	w := httptest.NewRecorder()
	httpx.Wrap(s.handleFederationNode).ServeHTTP(w, r)
	body := decodeDiscovery[struct {
		AuthorityNodeID string                   `json:"authority_node_id"`
		PublicKey       string                   `json:"public_key"`
		Descriptor      discovery.NodeDescriptor `json:"descriptor"`
	}](t, w, 200)
	if body.AuthorityNodeID != s.nodeIdentity.NodeID() || body.Descriptor.NodeID != s.nodeIdentity.NodeID() {
		t.Fatal("handshake identity does not match the configured node")
	}
	for _, want := range []string{"ddp-discovery/1", "ddp-client/1"} {
		if !slices.Contains(body.Descriptor.ProtocolVersions, want) {
			t.Fatalf("handshake no longer advertises %q: %v", want, body.Descriptor.ProtocolVersions)
		}
	}
	// The advertised descriptor must pass the registration check a peer would
	// run. The local id is swapped for a foreign one because self-registration
	// is a different rule than the protocol-version check under test.
	if err := body.Descriptor.Validate(body.PublicKey, "node-foreign", time.Now()); err != nil {
		t.Fatalf("advertised descriptor no longer validates: %v", err)
	}
}
