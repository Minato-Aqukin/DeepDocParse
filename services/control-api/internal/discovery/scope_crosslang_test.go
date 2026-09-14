package discovery

import (
	"testing"
	"time"
)

// TestScopeDigestCrossLanguageFixture freezes the digest of a manifest that
// carries child manifests and an HTML-escaped character. corpus-api
// re-derives this Go encoding in `_go_manifest_digest`; its test
// `test_go_produced_manifest_with_children_digest_is_accepted` pins the same
// value, so a drift on either side turns one of the two red.
func TestScopeDigestCrossLanguageFixture(t *testing.T) {
	when := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	node := "node-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	m := ScopeManifest{
		Schema: "ddp-scope-coverage/1#ScopeManifest", ScopeID: "go-scope-children",
		CallerScopeHash: "sha256:1111111111111111111111111111111111111111111111111111111111111111",
		CreatedAt:       when, ValidUntil: time.Date(2030, 1, 1, 0, 0, 0, 0, time.UTC),
		RegistryRevisionVector: []DirectoryRevision{
			{NodeID: node, RegistryRevision: 3, FetchedAt: when, DirectoryRef: "members", SnapshotRef: "snap-m"},
			{NodeID: "node-p", RegistryRevision: 4, FetchedAt: when, DirectoryRef: "collections", SnapshotRef: "snap-c"},
		},
		ChildManifests:     []ChildManifest{{NodeID: "node-p", ScopeRef: "snap-m", EnumerationState: "sealed"}},
		ExpandedMembers:    []TargetKey{{OriginNodeID: "node-p", CollectionID: "col<1>&", Operation: "corpus.retrieve"}},
		UnexpandedSubtrees: []UnknownSubtree{},
	}
	if err := FinalizeScope(&m); err != nil {
		t.Fatal(err)
	}
	const want = "sha256:2477d7718bc66f6ce793a05bc6020b9d8ef72e84eba88a0130b6091948704caf"
	if m.ManifestDigest != want {
		t.Fatalf("cross-language fixture digest drifted: %s", m.ManifestDigest)
	}
}
