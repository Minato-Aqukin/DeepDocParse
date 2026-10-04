package discovery

import (
	"testing"
	"time"
)

// TestScopeDigestCrossLanguageFixture freezes the digest of a manifest that
// carries child manifests, node routes and an HTML-escaped character. corpus-api
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
		NodeRoutes:         []NodeRoute{{NodeID: "node-r", ViaNodeIDs: []string{"node-p"}}},
		UnexpandedSubtrees: []UnknownSubtree{},
	}
	if err := FinalizeScope(&m); err != nil {
		t.Fatal(err)
	}
	const want = "sha256:7e4c2bb9f69a609dc965bb2e40491f4f890beed9f2b7706a67e10a26238eb99e"
	if m.ManifestDigest != want {
		t.Fatalf("cross-language fixture digest drifted: %s", m.ManifestDigest)
	}
}
