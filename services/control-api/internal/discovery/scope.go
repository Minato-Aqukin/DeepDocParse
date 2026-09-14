package discovery

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"slices"
	"strings"
	"time"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/contracts"
)

type TargetKey struct {
	OriginNodeID string `json:"origin_node_id"`
	CollectionID string `json:"collection_id"`
	Operation    string `json:"operation"`
}
type DirectoryRevision struct {
	NodeID           string    `json:"node_id"`
	RegistryRevision int64     `json:"registry_revision"`
	FetchedAt        time.Time `json:"fetched_at"`
	DirectoryRef     string    `json:"directory_ref,omitempty"`
	SnapshotRef      string    `json:"snapshot_ref,omitempty"`
}
type UnknownSubtree struct {
	NodeID string `json:"node_id"`
	Reason string `json:"reason"`
}
type ScopeManifest struct {
	Schema                 string              `json:"schema"`
	ScopeID                string              `json:"scope_id"`
	CallerScopeHash        string              `json:"caller_scope_hash"`
	CreatedAt              time.Time           `json:"created_at"`
	ValidUntil             time.Time           `json:"valid_until"`
	RegistryRevisionVector []DirectoryRevision `json:"registry_revision_vector"`
	ChildManifests         []ChildManifest     `json:"child_manifests,omitempty"`
	ExpandedMembers        []TargetKey         `json:"expanded_members"`
	UnexpandedSubtrees     []UnknownSubtree    `json:"unexpanded_subtrees"`
	EnumerationState       string              `json:"enumeration_state"`
	ManifestDigest         string              `json:"manifest_digest,omitempty"`
}
type ScopeEnvelope struct {
	Manifest                  ScopeManifest `json:"manifest"`
	FirstCursor               string        `json:"first_cursor"`
	TerminalCursor            string        `json:"terminal_cursor"`
	TotalTargets              int           `json:"total_targets"`
	ContentSnapshot           string        `json:"content_snapshot"`
	Expired                   bool          `json:"expired"`
	EffectiveEnumerationState string        `json:"effective_enumeration_state"`
}
type ScopeTarget struct {
	TargetKey TargetKey `json:"target_key"`
	State     string    `json:"state"`
}
type ScopeTargetPage struct {
	ScopeID        string        `json:"scope_id"`
	ManifestDigest string        `json:"manifest_digest"`
	Targets        []ScopeTarget `json:"targets"`
	NextCursor     *string       `json:"next_cursor"`
	Complete       bool          `json:"complete"`
	TotalTargets   int           `json:"total_targets"`
	Expired        bool          `json:"expired"`
}
type ScopeOptions struct {
	Operation        string `json:"operation"`
	MemberSnapshotID string `json:"member_snapshot_id,omitempty"`
	PageSize         int    `json:"page_size"`
	MaxMembers       int    `json:"max_members"`
	// MaxDiscoveryRequests bounds the outbound peer directory requests made while
	// collecting this scope. MaxRemoteMembers bounds how many distinct remote
	// nodes are contacted. Both default to a conservative non-zero value in the
	// handler and are enforced strictly; exhaustion marks the scope partial and
	// keeps everything already observed.
	MaxDiscoveryRequests int `json:"max_discovery_requests"`
	MaxRemoteMembers     int `json:"max_remote_members"`
	TTLSeconds           int `json:"ttl_seconds"`
}

func (o ScopeOptions) Validate() error {
	if strings.TrimSpace(o.Operation) != o.Operation || o.Operation == "" || len(o.Operation) > 100 || strings.ContainsAny(o.Operation, "\r\n\t") || o.PageSize < 1 || o.PageSize > 100 || o.MaxMembers < 1 || o.MaxMembers > 10000 || o.MaxDiscoveryRequests < 0 || o.MaxDiscoveryRequests > 10000 || o.MaxRemoteMembers < 0 || o.MaxRemoteMembers > 10000 || o.TTLSeconds < 30 || o.TTLSeconds > 3600 {
		return errors.New("invalid scope options")
	}
	return nil
}

// CollectionCatalog is server-owned input obtained through the configured corpus
// API. It must never be accepted from a public caller as a completeness assertion.
type CollectionCatalog struct {
	SnapshotID      string
	ScopeID         string
	CallerScopeHash string
	NodeID          string
	Revision        int64
	FetchedAt       time.Time
	ValidUntil      time.Time
	TerminalCursor  string
	Collections     []string
	Complete        bool
	FailureReason   string
}

// FinalizeScope normalizes the frozen denominator. Runtime capability readiness is
// intentionally not an input: missing/expired capability data cannot exclude targets.
func FinalizeScope(m *ScopeManifest) error {
	slices.SortFunc(m.ExpandedMembers, func(a, b TargetKey) int {
		if c := strings.Compare(a.OriginNodeID, b.OriginNodeID); c != 0 {
			return c
		}
		if c := strings.Compare(a.CollectionID, b.CollectionID); c != 0 {
			return c
		}
		return strings.Compare(a.Operation, b.Operation)
	})
	m.ExpandedMembers = slices.Compact(m.ExpandedMembers)
	if len(m.ChildManifests) > 0 {
		slices.SortFunc(m.ChildManifests, func(a, b ChildManifest) int {
			if c := strings.Compare(a.NodeID, b.NodeID); c != 0 {
				return c
			}
			if c := strings.Compare(a.ScopeRef, b.ScopeRef); c != 0 {
				return c
			}
			return strings.Compare(a.EnumerationState, b.EnumerationState)
		})
		m.ChildManifests = slices.Compact(m.ChildManifests)
	}
	slices.SortFunc(m.UnexpandedSubtrees, func(a, b UnknownSubtree) int {
		if c := strings.Compare(a.NodeID, b.NodeID); c != 0 {
			return c
		}
		return strings.Compare(a.Reason, b.Reason)
	})
	m.UnexpandedSubtrees = slices.Compact(m.UnexpandedSubtrees)
	m.EnumerationState = string(contracts.EnumerationStateSealed)
	if len(m.UnexpandedSubtrees) > 0 {
		m.EnumerationState = string(contracts.EnumerationStatePartial)
	}
	m.ManifestDigest = ""
	body, err := json.Marshal(m)
	if err != nil {
		return err
	}
	sum := sha256.Sum256(body)
	m.ManifestDigest = "sha256:" + hex.EncodeToString(sum[:])
	return nil
}
