package discovery

import (
	"crypto/sha256"
	"encoding/hex"
	"time"
)

// PeerScopeHash namespaces peer-directory snapshots apart from every user
// caller scope in the same member_snapshots table. It is derived from the
// serving node identity so a copied database cannot reuse another node's
// frozen peer views.
func PeerScopeHash(localID string) string {
	sum := sha256.Sum256([]byte("ddp-peer-directory|" + localID))
	return "sha256:" + hex.EncodeToString(sum[:])
}

// PeerView projects a frozen member into the fields a parent directory may see.
// Descriptor routes, public keys and sharing lists never appear.
func PeerView(m Member) PeerMember {
	p := PeerMember{
		NodeID:              m.NodeID,
		State:               m.State,
		Health:              m.Health,
		AcceptingAdmissions: m.AcceptingAdmissions,
		ExpansionState:      m.ExpansionState,
		Endpoints:           []Endpoint{},
	}
	if m.Descriptor != nil {
		p.DescriptorRevision = m.Descriptor.Revision
		p.ValidUntil = m.Descriptor.ValidUntil
		if m.Descriptor.ControlledEndpoints != nil {
			p.Endpoints = m.Descriptor.ControlledEndpoints
		}
	}
	return p
}

// PeerMember is the peer-facing projection of one approved direct member. It
// carries exactly what a parent directory needs to decide whether and how to
// expand the member: identity, descriptor revision, entry endpoints, current
// state and the declared expansion capability. Route records, public keys,
// organization sharing lists and subject lists never leave this node.
type PeerMember struct {
	NodeID              string     `json:"node_id"`
	DescriptorRevision  int64      `json:"descriptor_revision"`
	ValidUntil          time.Time  `json:"valid_until"`
	Endpoints           []Endpoint `json:"endpoints"`
	State               string     `json:"state"`
	Health              string     `json:"health"`
	AcceptingAdmissions bool       `json:"accepting_admissions"`
	ExpansionState      string     `json:"expansion_state"`
}

// Enumerable reports the declared member-enumeration capability. The serving
// directory encodes it in expansion_state: members that declared
// enumerate_members are `not_requested`; the rest are `unexpanded_subtree`.
func (m PeerMember) Enumerable() bool {
	return m.ExpansionState == ExpansionNotRequested
}

func (m PeerMember) descriptor() NodeDescriptor {
	return NodeDescriptor{
		Schema:              "ddp-discovery/1#NodeDescriptor",
		NodeID:              m.NodeID,
		ProtocolVersions:    []string{"ddp-discovery/1"},
		ControlledEndpoints: m.Endpoints,
		AuthMethods:         []string{"future_node_credentials"},
		DiscoveryCapabilities: DiscoveryCapabilities{
			EnumerateMembers: m.Enumerable(),
		},
		Revision:   m.DescriptorRevision,
		ValidUntil: m.ValidUntil,
	}
}

type PeerMemberPage struct {
	AuthorityNodeID  string       `json:"authority_node_id"`
	SnapshotID       string       `json:"snapshot_id"`
	RegistryRevision int64        `json:"registry_revision"`
	CreatedAt        time.Time    `json:"created_at"`
	ExpiresAt        time.Time    `json:"expires_at"`
	FirstCursor      string       `json:"first_cursor"`
	TerminalCursor   string       `json:"terminal_cursor"`
	Members          []PeerMember `json:"members"`
	NextCursor       *string      `json:"next_cursor"`
	Complete         bool         `json:"complete"`
}

func (p PeerMemberPage) valid(authority string) bool {
	if p.AuthorityNodeID != authority || p.SnapshotID == "" || len(p.SnapshotID) > 128 || p.RegistryRevision < 1 ||
		p.CreatedAt.IsZero() || p.ExpiresAt.IsZero() || !p.ExpiresAt.After(p.CreatedAt) ||
		p.FirstCursor == "" || p.TerminalCursor == "" || len(p.Members) > 100 {
		return false
	}
	if p.NextCursor != nil && *p.NextCursor == "" {
		return false
	}
	for _, member := range p.Members {
		if !peerNodePattern.MatchString(member.NodeID) {
			return false
		}
		if member.State != MemberApproved && member.State != MemberRevoked {
			return false
		}
	}
	return true
}

// CollectionRef is the minimal identity of a published collection. The full
// CollectionDescriptor stays in the response (the peer serves it verbatim) but
// the collector only needs the fixed key and origin identity.
type CollectionRef struct {
	CollectionID string `json:"collection_id"`
	OriginNodeID string `json:"origin_node_id"`
}

type PeerCatalogPage struct {
	AuthorityNodeID  string          `json:"authority_node_id"`
	SnapshotID       string          `json:"snapshot_id"`
	RegistryRevision int64           `json:"registry_revision"`
	CreatedAt        time.Time       `json:"created_at"`
	ValidUntil       time.Time       `json:"valid_until"`
	FirstCursor      string          `json:"first_cursor"`
	TerminalCursor   string          `json:"terminal_cursor"`
	Total            *int            `json:"total"`
	Collections      []CollectionRef `json:"collections"`
	NextCursor       *string         `json:"next_cursor"`
	Complete         bool            `json:"complete"`
}

func (p PeerCatalogPage) valid(authority string) bool {
	if p.AuthorityNodeID != authority || p.SnapshotID == "" || len(p.SnapshotID) > 128 || p.RegistryRevision < 1 ||
		p.CreatedAt.IsZero() || p.ValidUntil.IsZero() || !p.ValidUntil.After(p.CreatedAt) ||
		p.FirstCursor == "" || p.TerminalCursor == "" || p.Total == nil || *p.Total < 0 || *p.Total > 10000 ||
		len(p.Collections) > 100 {
		return false
	}
	if p.NextCursor != nil && *p.NextCursor == "" {
		return false
	}
	for _, collection := range p.Collections {
		if collection.CollectionID == "" || collection.OriginNodeID != authority {
			return false
		}
	}
	return true
}
