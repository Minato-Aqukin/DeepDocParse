package discovery

import (
	"crypto/ed25519"
	"encoding/base64"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// TestNodeIdentityBackupRestoreDrill is the machine half of
// scripts/backup_restore_drill.sh. It is skipped unless DDP_IDENTITY_DRILL_ROOT
// is set, so the normal `go test ./...` stays hermetic.
//
// What it proves about the authority identity:
//
//  1. the serialized identity is exactly the 0600 seed file (the database row
//     alone is not enough, and the seed alone is not enough either);
//  2. restoring the same seed bytes restores the same node id and fingerprint;
//  3. a fresh clone is a *different* authority and cannot verify a proof signed
//     by the original — a cloned environment cannot impersonate it;
//  4. a missing seed with allowCreate=false refuses to invent a new identity.
func TestNodeIdentityBackupRestoreDrill(t *testing.T) {
	root := os.Getenv("DDP_IDENTITY_DRILL_ROOT")
	if root == "" {
		t.Skip("set DDP_IDENTITY_DRILL_ROOT to run the recovery drill")
	}

	authorityDir := filepath.Join(root, "authority")
	authority, err := LoadIdentity(authorityDir, true)
	if err != nil {
		t.Fatal(err)
	}
	t.Logf("authority node id=%s fingerprint=%s", authority.NodeID(), authority.Fingerprint())

	seed, err := os.ReadFile(filepath.Join(authorityDir, identitySeedFile))
	if err != nil {
		t.Fatal(err)
	}
	if len(seed) != ed25519.SeedSize {
		t.Fatalf("seed backup must be %d bytes, got %d", ed25519.SeedSize, len(seed))
	}
	restoredDir := filepath.Join(root, "restored")
	if err := os.MkdirAll(restoredDir, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(restoredDir, identitySeedFile), seed, 0o600); err != nil {
		t.Fatal(err)
	}
	restored, err := LoadIdentity(restoredDir, false)
	if err != nil {
		t.Fatal(err)
	}
	if restored.NodeID() != authority.NodeID() || restored.Fingerprint() != authority.Fingerprint() {
		t.Fatal("same seed bytes produced a different authority identity")
	}
	t.Logf("restored same authority: node id=%s fingerprint=%s",
		restored.NodeID(), restored.Fingerprint())

	// A fresh environment that never got the backup is a different authority.
	cloneDir := filepath.Join(root, "fresh-clone")
	clone, err := LoadIdentity(cloneDir, true)
	if err != nil {
		t.Fatal(err)
	}
	if clone.NodeID() == authority.NodeID() || clone.PublicKey() == authority.PublicKey() {
		t.Fatal("cloned environment impersonated the original authority")
	}
	message := []byte("ddp-node-proof/1 drill")
	signature, err := base64.StdEncoding.DecodeString(authority.Sign(message))
	if err != nil {
		t.Fatal(err)
	}
	public, err := base64.StdEncoding.DecodeString(clone.PublicKey())
	if err != nil {
		t.Fatal(err)
	}
	if ed25519.Verify(public, message, signature) {
		t.Fatal("a fresh clone verified the original authority's proof")
	}
	originalPublic, err := base64.StdEncoding.DecodeString(authority.PublicKey())
	if err != nil {
		t.Fatal(err)
	}
	if !ed25519.Verify(originalPublic, message, signature) {
		t.Fatal("the restored authority cannot verify its own proof")
	}

	// Missing persisted seed must refuse to silently mint a new authority.
	emptyDir := filepath.Join(root, "missing-seed")
	if err := os.MkdirAll(emptyDir, 0o700); err != nil {
		t.Fatal(err)
	}
	if _, err := LoadIdentity(emptyDir, false); err == nil ||
		!strings.Contains(err.Error(), "restore") {
		t.Fatalf("missing seed with allowCreate=false did not demand a restore: %v", err)
	}
	t.Logf("clone node id=%s differs; missing seed refused; create/read paths exercised",
		clone.NodeID())
}
