package discovery

import (
	"bytes"
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
)

func identityDirectory(t *testing.T) string {
	t.Helper()
	return filepath.Join(t.TempDir(), "node")
}

func TestIdentitySurvivesRestartAndDirectoryMove(t *testing.T) {
	dir := identityDirectory(t)
	first, err := LoadIdentity(dir, true)
	if err != nil {
		t.Fatal(err)
	}
	restarted, err := LoadIdentity(dir, false)
	if err != nil {
		t.Fatal(err)
	}
	moved := filepath.Join(filepath.Dir(dir), "restored-node")
	if err := os.Rename(dir, moved); err != nil {
		t.Fatal(err)
	}
	restored, err := LoadIdentity(moved, false)
	if err != nil {
		t.Fatal(err)
	}
	for _, identity := range []*Identity{restarted, restored} {
		if identity.NodeID() != first.NodeID() || identity.PublicKey() != first.PublicKey() || identity.Fingerprint() != first.Fingerprint() {
			t.Fatal("persistent identity changed after restart or restore")
		}
	}
	if len(first.NodeID()) != 53 || !strings.HasPrefix(first.NodeID(), "node-") || len(first.Fingerprint()) != 71 {
		t.Fatal("unexpected identity or fingerprint format")
	}
	for path, mode := range map[string]os.FileMode{moved: 0700, filepath.Join(moved, identitySeedFile): 0600} {
		info, err := os.Stat(path)
		if err != nil || info.Mode().Perm() != mode {
			t.Fatalf("unexpected permissions for %s", path)
		}
	}
	public, _ := base64.StdEncoding.DecodeString(first.PublicKey())
	message := []byte("descriptor without private data")
	signature, err := base64.StdEncoding.DecodeString(first.Sign(message))
	if err != nil || !ed25519.Verify(public, message, signature) {
		t.Fatal("signature does not verify with published public key")
	}
}

func TestIdentityRejectsMissingPersistedKey(t *testing.T) {
	dir := identityDirectory(t)
	if _, err := LoadIdentity(dir, false); err == nil {
		t.Fatal("missing identity directory silently created")
	}
	if _, err := LoadIdentity(dir, true); err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(filepath.Join(dir, identitySeedFile)); err != nil {
		t.Fatal(err)
	}
	if _, err := LoadIdentity(dir, false); err == nil {
		t.Fatal("missing persisted key silently replaced")
	}
	if _, err := os.Stat(filepath.Join(dir, identitySeedFile)); !os.IsNotExist(err) {
		t.Fatal("a replacement key was written")
	}
}

func TestIdentityConcurrentFirstStartPublishesOneCompleteKey(t *testing.T) {
	dir := identityDirectory(t)
	const initializers = 48
	var wg sync.WaitGroup
	identities := make([]*Identity, initializers)
	errors := make([]error, initializers)
	start := make(chan struct{})
	for n := range initializers {
		wg.Add(1)
		go func() {
			defer wg.Done()
			<-start
			identities[n], errors[n] = LoadIdentity(dir, true)
		}()
	}
	close(start)
	wg.Wait()
	for n, err := range errors {
		if err != nil {
			t.Fatalf("initializer %d: %v", n, err)
		}
		if identities[n].NodeID() != identities[0].NodeID() {
			t.Fatal("concurrent first starts published different identities")
		}
	}
	entries, err := os.ReadDir(dir)
	if err != nil || len(entries) != 1 || entries[0].Name() != identitySeedFile {
		t.Fatal("unexpected temporary identity files remain")
	}
}

func TestIdentityRejectsInsecurePermissions(t *testing.T) {
	for _, target := range []string{"directory", "seed"} {
		t.Run(target, func(t *testing.T) {
			dir := identityDirectory(t)
			if _, err := LoadIdentity(dir, true); err != nil {
				t.Fatal(err)
			}
			path, mode := dir, os.FileMode(0755)
			if target == "seed" {
				path, mode = filepath.Join(dir, identitySeedFile), 0640
			}
			if err := os.Chmod(path, mode); err != nil {
				t.Fatal(err)
			}
			if _, err := LoadIdentity(dir, true); err == nil {
				t.Fatal("insecure permissions accepted")
			}
		})
	}
}

func TestIdentityRejectsMalformedSeeds(t *testing.T) {
	for _, size := range []int{0, 31, 33, 64, 4096} {
		t.Run(fmt.Sprint(size), func(t *testing.T) {
			dir := identityDirectory(t)
			if err := os.Mkdir(dir, 0700); err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(filepath.Join(dir, identitySeedFile), bytes.Repeat([]byte{17}, size), 0600); err != nil {
				t.Fatal(err)
			}
			if _, err := LoadIdentity(dir, true); err == nil {
				t.Fatal("malformed persisted key accepted or replaced")
			}
		})
	}
}

func TestIdentityRejectsSymbolicLinks(t *testing.T) {
	for _, target := range []string{"directory", "ancestor", "seed"} {
		t.Run(target, func(t *testing.T) {
			dir := identityDirectory(t)
			if _, err := LoadIdentity(dir, true); err != nil {
				t.Fatal(err)
			}
			switch target {
			case "directory":
				link := filepath.Join(filepath.Dir(dir), "linked")
				if err := os.Symlink(dir, link); err != nil {
					t.Fatal(err)
				}
				dir = link
			case "ancestor":
				link := filepath.Join(t.TempDir(), "linked-parent")
				if err := os.Symlink(filepath.Dir(dir), link); err != nil {
					t.Fatal(err)
				}
				dir = filepath.Join(link, "node")
			case "seed":
				seed := filepath.Join(dir, identitySeedFile)
				backup := filepath.Join(dir, "backup.seed")
				if err := os.Rename(seed, backup); err != nil {
					t.Fatal(err)
				}
				if err := os.Symlink(backup, seed); err != nil {
					t.Fatal(err)
				}
			}
			if _, err := LoadIdentity(dir, true); err == nil {
				t.Fatal("symbolic link accepted")
			}
		})
	}
}

func TestNodeIDForPublicKeyValidatesCanonicalEncoding(t *testing.T) {
	identity, err := LoadIdentity(identityDirectory(t), true)
	if err != nil {
		t.Fatal(err)
	}
	got, err := NodeIDForPublicKey(identity.PublicKey())
	if err != nil || got != identity.NodeID() {
		t.Fatal("public key derived another node ID")
	}
	for _, encoded := range []string{"", "invalid", identity.PublicKey() + "\n", base64.StdEncoding.EncodeToString(make([]byte, 31)), base64.StdEncoding.EncodeToString(make([]byte, 33))} {
		if _, err := NodeIDForPublicKey(encoded); err == nil {
			t.Fatal("invalid public key accepted")
		}
	}
}

func TestIdentitySerializationAndFormattingDoNotExposePrivateKey(t *testing.T) {
	identity, err := LoadIdentity(identityDirectory(t), true)
	if err != nil {
		t.Fatal(err)
	}
	for _, value := range []any{identity, *identity} {
		encoded, err := json.Marshal(value)
		if err != nil || string(encoded) != "{}" {
			t.Fatal("identity unexpectedly exports serialized fields")
		}
		for _, format := range []string{"%v", "%+v", "%#v", "%s"} {
			if fmt.Sprintf(format, value) != identity.NodeID() {
				t.Fatal("identity formatting exposes more than public node ID")
			}
		}
	}
}
