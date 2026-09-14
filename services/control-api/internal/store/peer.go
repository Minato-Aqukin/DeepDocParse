package store

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"time"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/auth"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/discovery"
	"github.com/jackc/pgx/v5"
)

// AuthorizedSnapshotMembers walks a caller's frozen member snapshot through the
// same authorization overlay MemberSnapshotPage applies and returns the whole
// list plus whether the stored cursor chain reached its terminal page. Only a
// complete chain may seed remote expansion; an inconsistent chain stays with the
// store's existing unknown-subtree handling.
func (s *Store) AuthorizedSnapshotMembers(ctx context.Context, org, subject, scope, id string, admin bool) ([]discovery.Member, bool, error) {
	out := []discovery.Member{}
	cursor := ""
	seen := map[string]bool{}
	for {
		page, err := s.MemberSnapshotPage(ctx, org, subject, scope, id, cursor, admin)
		if err != nil {
			return nil, false, err
		}
		out = append(out, page.Members...)
		if page.Complete {
			return out, true, nil
		}
		if page.NextCursor == nil || *page.NextCursor == "" || seen[*page.NextCursor] || len(out) > 10000 {
			return out, false, nil
		}
		seen[*page.NextCursor] = true
		cursor = *page.NextCursor
	}
}

// CreatePeerMemberSnapshot freezes the approved, organization-visible direct
// members for peer directory reads. Hidden and pending members never enter the
// page; the scope is derived from this node, never from the caller.
func (s *Store) CreatePeerMemberSnapshot(ctx context.Context, org, localID string, pageSize int, ttl time.Duration) (*discovery.Snapshot, error) {
	if pageSize < 1 || pageSize > 100 || ttl < 30*time.Second || ttl > time.Hour {
		return nil, ErrDiscoveryConflict
	}
	snap := &discovery.Snapshot{ID: auth.NewID(), AuthorityNodeID: localID, CallerScopeHash: discovery.PeerScopeHash(localID)}
	err := s.InTx(ctx, func(tx pgx.Tx) error {
		if _, err := lockDirectory(ctx, tx, org); err != nil {
			return err
		}
		if err := tx.QueryRow(ctx, `SELECT clock_timestamp()`).Scan(&snap.CreatedAt); err != nil {
			return err
		}
		snap.ExpiresAt = snap.CreatedAt.Add(ttl)
		rows, err := tx.Query(ctx, `SELECT descriptor,state,public_revision FROM control.node_members WHERE organization_id=$1 AND state='approved' AND visible_to_org ORDER BY node_id`, org)
		if err != nil {
			return err
		}
		members := []discovery.Member{}
		for rows.Next() {
			var raw []byte
			var state string
			var memberRev int64
			if err = rows.Scan(&raw, &state, &memberRev); err != nil {
				rows.Close()
				return err
			}
			var d discovery.NodeDescriptor
			if err = json.Unmarshal(raw, &d); err != nil {
				rows.Close()
				return err
			}
			members = append(members, discovery.DirectMember(d, state, memberRev, localID, snap.CreatedAt))
		}
		rows.Close()
		if err = rows.Err(); err != nil {
			return err
		}
		view := make([]any, 0, len(members))
		for _, m := range members {
			view = append(view, []any{m.NodeID, m.State, m.Revision, m.Descriptor})
		}
		viewBody, err := json.Marshal(view)
		if err != nil {
			return err
		}
		digest := sha256.Sum256(viewBody)
		err = tx.QueryRow(ctx, `INSERT INTO control.node_directory_views(organization_id,caller_scope_hash,fingerprint) VALUES($1,$2,$3)
 ON CONFLICT(organization_id,caller_scope_hash) DO UPDATE SET
 revision=CASE WHEN control.node_directory_views.fingerprint=excluded.fingerprint THEN control.node_directory_views.revision ELSE control.node_directory_views.revision+1 END,
 fingerprint=excluded.fingerprint RETURNING revision`, org, snap.CallerScopeHash, hex.EncodeToString(digest[:])).Scan(&snap.RegistryRevision)
		if err != nil {
			return err
		}
		pageCount := (len(members) + pageSize - 1) / pageSize
		cursors := make([]string, pageCount+1)
		for i := range cursors {
			cursors[i] = auth.NewID()
		}
		snap.FirstCursor = cursors[0]
		snap.TerminalCursor = cursors[pageCount]
		_, err = tx.Exec(ctx, `INSERT INTO control.member_snapshots(id,organization_id,authority_node_id,caller_scope_hash,registry_revision,created_at,expires_at,first_cursor,terminal_cursor,page_size) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)`, snap.ID, org, localID, snap.CallerScopeHash, snap.RegistryRevision, snap.CreatedAt, snap.ExpiresAt, snap.FirstCursor, snap.TerminalCursor, pageSize)
		if err != nil {
			return err
		}
		for i, cursor := range cursors {
			page := []discovery.Member{}
			var next *string
			if i < pageCount {
				end := min((i+1)*pageSize, len(members))
				page = members[i*pageSize : end]
				next = &cursors[i+1]
			}
			body, err := json.Marshal(page)
			if err != nil {
				return err
			}
			_, err = tx.Exec(ctx, `INSERT INTO control.member_snapshot_pages(snapshot_id,cursor,next_cursor,members) VALUES($1,$2,$3,$4)`, snap.ID, cursor, next, body)
			if err != nil {
				return err
			}
		}
		return nil
	})
	return snap, err
}

// PeerMemberSnapshotPage reads one stable peer directory page. Continuation
// cannot change the page size, and revocation overlays are applied before any
// descriptor field is projected.
func (s *Store) PeerMemberSnapshotPage(ctx context.Context, org, localID, snapshotID, cursor string, limit int) (*discovery.PeerMemberPage, error) {
	out := &discovery.PeerMemberPage{}
	scope := discovery.PeerScopeHash(localID)
	var pageSize int
	var expired bool
	var callerScope string
	err := s.pool.QueryRow(ctx, `SELECT id,authority_node_id,caller_scope_hash,registry_revision,created_at,expires_at,first_cursor,terminal_cursor,page_size,expires_at<=clock_timestamp() FROM control.member_snapshots WHERE id=$1 AND organization_id=$2 AND caller_scope_hash=$3`, snapshotID, org, scope).Scan(&out.SnapshotID, &out.AuthorityNodeID, &callerScope, &out.RegistryRevision, &out.CreatedAt, &out.ExpiresAt, &out.FirstCursor, &out.TerminalCursor, &pageSize, &expired)
	if err != nil {
		return nil, norows(err)
	}
	if expired {
		return nil, ErrSnapshotExpired
	}
	if limit > 0 && limit != pageSize {
		return nil, ErrDiscoveryConflict
	}
	if cursor == "" {
		cursor = out.FirstCursor
	}
	var body []byte
	var next *string
	err = s.pool.QueryRow(ctx, `SELECT members,next_cursor FROM control.member_snapshot_pages WHERE snapshot_id=$1 AND cursor=$2`, snapshotID, cursor).Scan(&body, &next)
	if err != nil {
		return nil, norows(err)
	}
	var members []discovery.Member
	if err = json.Unmarshal(body, &members); err != nil {
		return nil, err
	}
	out.Members = []discovery.PeerMember{}
	for i := range members {
		m := &members[i]
		var state string
		var visible bool
		var authorizationRevision int64
		err = s.pool.QueryRow(ctx, `SELECT state,visible_to_org,public_revision FROM control.node_members WHERE organization_id=$1 AND node_id=$2`, org, m.NodeID).Scan(&state, &visible, &authorizationRevision)
		if err != nil && !errors.Is(err, pgx.ErrNoRows) {
			return nil, err
		}
		if err != nil || state != discovery.MemberApproved || !visible || authorizationRevision != m.Revision {
			m.State = discovery.MemberRevoked
			m.Descriptor = nil
			m.Route = nil
			m.Configured = false
			m.ExpansionState = discovery.ExpansionRevoked
		}
		out.Members = append(out.Members, discovery.PeerView(*m))
	}
	out.NextCursor = next
	out.Complete = cursor == out.TerminalCursor
	return out, nil
}
