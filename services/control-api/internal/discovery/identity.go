// Package discovery implements the control domain's managed node directory.
package discovery

import (
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
)

const identitySeedFile = "ed25519.seed"

// Identity is a persistent signing identity. The private key is intentionally
// unexported: neither JSON serialization nor ordinary formatting exposes it.
type Identity struct {
	private ed25519.PrivateKey
	public  ed25519.PublicKey
}

func (i *Identity) NodeID() string { return nodeID(i.public) }

func (i *Identity) PublicKey() string { return base64.StdEncoding.EncodeToString(i.public) }

func (i *Identity) Fingerprint() string {
	digest := sha256.Sum256(i.public)
	return "sha256:" + hex.EncodeToString(digest[:])
}

func (i *Identity) Sign(value []byte) string {
	return base64.StdEncoding.EncodeToString(ed25519.Sign(i.private, value))
}

func (i Identity) String() string   { return nodeID(i.public) }
func (i Identity) GoString() string { return nodeID(i.public) }

func nodeID(public ed25519.PublicKey) string {
	digest := sha256.Sum256(public)
	return "node-" + hex.EncodeToString(digest[:])[:48]
}

// NodeIDForPublicKey validates an Ed25519 public key and derives its immutable
// authority identity. Addresses and display names never participate in this ID.
func NodeIDForPublicKey(encoded string) (string, error) {
	public, err := base64.StdEncoding.Strict().DecodeString(encoded)
	if err != nil || len(public) != ed25519.PublicKeySize || base64.StdEncoding.EncodeToString(public) != encoded {
		return "", errors.New("invalid Ed25519 public key")
	}
	return nodeID(public), nil
}

// LoadIdentity opens a durable Ed25519 seed. allowCreate must be false once a
// public identity has been registered in the control database: a missing key
// must require recovery, never silently turn the installation into a new node.
// Backup and restore the directory together with the control database.
func LoadIdentity(dir string, allowCreate bool) (*Identity, error) {
	root, err := openIdentityDirectory(dir, allowCreate)
	if err != nil {
		return nil, err
	}
	defer root.Close()

	identity, err := readIdentity(root)
	if err == nil || !errors.Is(err, os.ErrNotExist) {
		return identity, err
	}
	if !allowCreate {
		return nil, errors.New("node identity seed is missing; restore the persisted identity")
	}
	if err := createIdentity(root); err != nil {
		return nil, err
	}
	return readIdentity(root)
}

func openIdentityDirectory(dir string, create bool) (*os.Root, error) {
	if strings.TrimSpace(dir) == "" {
		return nil, errors.New("node identity directory is required")
	}
	abs, err := filepath.Abs(dir)
	if err != nil {
		return nil, errors.New("invalid node identity directory")
	}
	// Reject symlinks in every component, including ancestors. Mkdir only creates
	// missing components; existing directory permissions are never broadened.
	current := string(filepath.Separator)
	for _, part := range strings.Split(strings.TrimPrefix(abs, current), string(filepath.Separator)) {
		if part == "" {
			continue
		}
		current = filepath.Join(current, part)
		info, statErr := os.Lstat(current)
		if errors.Is(statErr, os.ErrNotExist) && create {
			mkdirErr := os.Mkdir(current, 0700)
			if mkdirErr != nil && !errors.Is(mkdirErr, os.ErrExist) {
				return nil, errors.New("cannot create node identity directory")
			}
			if mkdirErr == nil {
				parent, err := os.Open(filepath.Dir(current))
				if err != nil {
					return nil, errors.New("cannot sync node identity directory creation")
				}
				err = parent.Sync()
				parent.Close()
				if err != nil {
					return nil, errors.New("cannot sync node identity directory creation")
				}
			}
			info, statErr = os.Lstat(current)
		}
		if statErr != nil {
			return nil, errors.New("node identity directory is missing or inaccessible")
		}
		if !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
			return nil, errors.New("node identity directory must not contain symbolic links")
		}
	}
	before, err := os.Lstat(abs)
	if err != nil || !before.IsDir() || before.Mode().Perm() != 0700 || before.Mode()&(os.ModeSetuid|os.ModeSetgid|os.ModeSticky) != 0 {
		return nil, errors.New("node identity directory must have mode 0700")
	}
	root, err := os.OpenRoot(abs)
	if err != nil {
		return nil, errors.New("cannot open node identity directory")
	}
	after, err := root.Stat(".")
	if err != nil || !os.SameFile(before, after) {
		root.Close()
		return nil, errors.New("node identity directory changed while opening")
	}
	return root, nil
}

func readIdentity(root *os.Root) (*Identity, error) {
	before, err := root.Lstat(identitySeedFile)
	if err != nil {
		return nil, fmt.Errorf("cannot inspect node identity seed: %w", err)
	}
	if !before.Mode().IsRegular() || before.Mode().Perm() != 0600 || before.Mode()&(os.ModeSetuid|os.ModeSetgid|os.ModeSticky) != 0 {
		return nil, errors.New("node identity seed must be a regular file with mode 0600")
	}
	file, err := root.Open(identitySeedFile)
	if err != nil {
		return nil, errors.New("cannot open node identity seed")
	}
	defer file.Close()
	after, err := file.Stat()
	if err != nil || !os.SameFile(before, after) || after.Mode().Perm() != 0600 {
		return nil, errors.New("node identity seed changed while opening")
	}
	// Inspect again after opening so a replaced symbolic link is rejected even
	// when its target happens to be the original inode. Never read before checks.
	linked, err := root.Lstat(identitySeedFile)
	if err != nil || !linked.Mode().IsRegular() || !os.SameFile(after, linked) {
		return nil, errors.New("node identity seed changed while opening")
	}
	seed, err := io.ReadAll(io.LimitReader(file, ed25519.SeedSize+1))
	if err != nil || len(seed) != ed25519.SeedSize {
		return nil, errors.New("node identity seed must contain exactly 32 bytes")
	}
	private := ed25519.NewKeyFromSeed(seed)
	clear(seed)
	return &Identity{private: private, public: private.Public().(ed25519.PublicKey)}, nil
}

func createIdentity(root *os.Root) error {
	seed := make([]byte, ed25519.SeedSize)
	if _, err := rand.Read(seed); err != nil {
		return errors.New("cannot generate node identity")
	}
	defer clear(seed)
	var suffix [16]byte
	if _, err := rand.Read(suffix[:]); err != nil {
		return errors.New("cannot generate node identity temporary filename")
	}
	temporary := ".ed25519-" + hex.EncodeToString(suffix[:]) + ".tmp"
	file, err := root.OpenFile(temporary, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	if err != nil {
		return errors.New("cannot create node identity seed")
	}
	defer root.Remove(temporary)
	defer file.Close()
	if err := file.Chmod(0600); err != nil {
		return errors.New("cannot secure node identity seed")
	}
	if _, err := file.Write(seed); err != nil {
		return errors.New("cannot write node identity seed")
	}
	if err := file.Sync(); err != nil {
		return errors.New("cannot sync node identity seed")
	}
	if err := file.Close(); err != nil {
		return errors.New("cannot close node identity seed")
	}
	// Linking a complete, synced file publishes it without replacing an existing
	// identity. Concurrent initializers all read the one successful publication.
	if err := root.Link(temporary, identitySeedFile); err != nil && !errors.Is(err, os.ErrExist) {
		return errors.New("cannot publish node identity seed")
	}
	if err := root.Remove(temporary); err != nil {
		return errors.New("cannot remove node identity temporary file")
	}
	directory, err := root.Open(".")
	if err != nil {
		return errors.New("cannot open node identity directory for sync")
	}
	defer directory.Close()
	if err := directory.Sync(); err != nil {
		return errors.New("cannot sync node identity directory")
	}
	return nil
}
