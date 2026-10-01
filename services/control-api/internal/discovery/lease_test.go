package discovery

import (
	"encoding/base64"
	"path/filepath"
	"testing"
	"time"
)

func TestDescriptorSignatureUsesWholeSecondUTC(t *testing.T) {
	identity, err := LoadIdentity(filepath.Join(t.TempDir(), "node"), true)
	if err != nil {
		t.Fatal(err)
	}
	descriptor := NodeDescriptor{Schema: "ddp-discovery/1#NodeDescriptor", NodeID: identity.NodeID(), ProtocolVersions: []string{"ddp-discovery/1"}, ControlledEndpoints: []Endpoint{{Purpose: "federation", URL: "https://peer.example/api/v1/federation"}}, AuthMethods: []string{"session"}, Revision: 1, ValidUntil: time.Date(2026, 10, 2, 12, 30, 0, 123456789, time.FixedZone("offset", 3600))}
	if err := identity.SignDescriptor(&descriptor); err != nil {
		t.Fatal(err)
	}
	if descriptor.ValidUntil.Nanosecond() != 0 || descriptor.ValidUntil.Format(time.RFC3339) != "2026-10-02T11:30:00Z" {
		t.Fatalf("signed lease has noncanonical time: %s", descriptor.ValidUntil.Format(time.RFC3339Nano))
	}
	if err := descriptor.VerifyPublisher(identity.PublicKey()); err != nil {
		t.Fatal(err)
	}
	descriptor.ValidUntil = descriptor.ValidUntil.Add(time.Nanosecond)
	if err := descriptor.VerifyPublisher(identity.PublicKey()); err == nil {
		t.Fatal("fractional timestamp accepted in a whole-second signed lease")
	}
}

func TestSignedDescriptorRejectsNonASCIIFields(t *testing.T) {
	identity, err := LoadIdentity(filepath.Join(t.TempDir(), "node"), true)
	if err != nil {
		t.Fatal(err)
	}
	descriptor := NodeDescriptor{Schema: "ddp-discovery/1#NodeDescriptor", NodeID: identity.NodeID(), ProtocolVersions: []string{"ddp-discovery/1"}, ControlledEndpoints: []Endpoint{{Purpose: "federation", URL: "https://peer.example/api/v1/federation"}}, AuthMethods: []string{"session\u2028user"}, Revision: 1, ValidUntil: time.Now().UTC().Truncate(time.Second).Add(time.Minute), PublisherSignature: base64.StdEncoding.EncodeToString(make([]byte, 64))}
	if err := descriptor.Validate(identity.PublicKey(), "other-node", time.Now()); err == nil {
		t.Fatal("non-ASCII signed descriptor accepted despite differing Go/JavaScript JSON escapes")
	}
}
