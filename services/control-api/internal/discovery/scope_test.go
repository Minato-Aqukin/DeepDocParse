package discovery

import (
	"encoding/json"
	"strings"
	"testing"
	"time"
)

func TestScopeDigestDeduplicatesOriginCollectionOperationAndSurvivesJSONBOrder(t *testing.T) {
	when := time.Date(2026, 9, 13, 1, 0, 0, 0, time.UTC)
	m := ScopeManifest{Schema: "ddp-scope-coverage/1#ScopeManifest", ScopeID: "fixed-scope", CallerScopeHash: "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef", CreatedAt: when, ValidUntil: when.Add(time.Hour), RegistryRevisionVector: []DirectoryRevision{{NodeID: "origin-one", RegistryRevision: 1, FetchedAt: when, DirectoryRef: "collections", SnapshotRef: "snapshot-one"}}, ExpandedMembers: []TargetKey{{"origin-one", "collection-b", "search"}, {"origin-one", "collection-a", "search"}, {"origin-one", "collection-a", "search"}, {"origin-one", "collection-a", "rag.answer.cited"}}, UnexpandedSubtrees: []UnknownSubtree{}}
	if err := FinalizeScope(&m); err != nil {
		t.Fatal(err)
	}
	if len(m.ExpandedMembers) != 3 || m.EnumerationState != "sealed" {
		t.Fatalf("stable key omitted operation or counted duplicate paths %+v", m)
	}
	digest := m.ManifestDigest
	body, _ := json.Marshal(m)
	var fields map[string]json.RawMessage
	json.Unmarshal(body, &fields)
	reordered, _ := json.Marshal(fields)
	var restored ScopeManifest
	if err := json.Unmarshal(reordered, &restored); err != nil {
		t.Fatal(err)
	}
	if err := FinalizeScope(&restored); err != nil {
		t.Fatal(err)
	}
	if restored.ManifestDigest != digest {
		t.Fatal("stored JSON key order changed reproducible digest")
	}
	restored.UnexpandedSubtrees = append(restored.UnexpandedSubtrees, UnknownSubtree{NodeID: "origin-two", Reason: "unknown"})
	FinalizeScope(&restored)
	if restored.ManifestDigest == digest || restored.EnumerationState != "partial" {
		t.Fatal("unknown subtree was omitted from digest or seal predicate")
	}
}

func TestScopeDigestCoversChildManifestsDeterministically(t *testing.T) {
	when := time.Date(2026, 9, 13, 1, 0, 0, 0, time.UTC)
	m := ScopeManifest{Schema: "ddp-scope-coverage/1#ScopeManifest", ScopeID: "child-scope", CallerScopeHash: "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef", CreatedAt: when, ValidUntil: when.Add(time.Hour),
		RegistryRevisionVector: []DirectoryRevision{{NodeID: "origin-one", RegistryRevision: 1, FetchedAt: when, DirectoryRef: "members", SnapshotRef: "snapshot-one"}},
		ChildManifests:         []ChildManifest{{"origin-two", "snap-b", "partial"}, {"origin-one", "snap-a", "sealed"}, {"origin-one", "snap-a", "sealed"}},
		ExpandedMembers:        []TargetKey{}, UnexpandedSubtrees: []UnknownSubtree{}}
	if err := FinalizeScope(&m); err != nil {
		t.Fatal(err)
	}
	if len(m.ChildManifests) != 2 || m.ChildManifests[0].NodeID != "origin-one" || m.ChildManifests[1].NodeID != "origin-two" || m.EnumerationState != "sealed" {
		t.Fatalf("child manifests not sorted/deduped or changed the seal predicate: %+v", m.ChildManifests)
	}
	digest := m.ManifestDigest
	body, _ := json.Marshal(m)
	if !strings.Contains(string(body), `"child_manifests"`) {
		t.Fatal("non-empty child manifests must appear in the frozen manifest")
	}
	var fields map[string]json.RawMessage
	json.Unmarshal(body, &fields)
	reordered, _ := json.Marshal(fields)
	var restored ScopeManifest
	if err := json.Unmarshal(reordered, &restored); err != nil {
		t.Fatal(err)
	}
	if err := FinalizeScope(&restored); err != nil {
		t.Fatal(err)
	}
	if restored.ManifestDigest != digest {
		t.Fatal("child manifests changed reproducible digest across JSON key order")
	}
	restored.ChildManifests[0].EnumerationState = "partial"
	FinalizeScope(&restored)
	if restored.ManifestDigest == digest {
		t.Fatal("child manifest state is not covered by the digest")
	}
	empty := ScopeManifest{Schema: "ddp-scope-coverage/1#ScopeManifest", ScopeID: "no-children", CallerScopeHash: m.CallerScopeHash, CreatedAt: when, ValidUntil: when.Add(time.Hour),
		RegistryRevisionVector: m.RegistryRevisionVector, ExpandedMembers: []TargetKey{}, UnexpandedSubtrees: []UnknownSubtree{}}
	if err := FinalizeScope(&empty); err != nil {
		t.Fatal(err)
	}
	emptyBody, _ := json.Marshal(empty)
	if strings.Contains(string(emptyBody), "child_manifests") {
		t.Fatal("child-free manifests must stay byte-compatible with the pre-expansion digest preimage")
	}
}
