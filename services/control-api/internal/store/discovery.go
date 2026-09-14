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

var ErrDiscoveryConflict = errors.New("discovery revision or state conflict")
var ErrSnapshotExpired = errors.New("member snapshot expired")
var ErrNodeIdentityMismatch = errors.New("persistent node identity mismatch; restore matching identity directory")

type PublicNodeIdentity struct {
	NodeID      string
	PublicKey   string
	Fingerprint string
}

func (s *Store) NodeIdentity(ctx context.Context) (*PublicNodeIdentity, error) {
	var v PublicNodeIdentity
	err := s.pool.QueryRow(ctx, `SELECT node_id,public_key,key_fingerprint FROM control.node_identity WHERE singleton`).Scan(&v.NodeID, &v.PublicKey, &v.Fingerprint)
	return &v, norows(err)
}
func (s *Store) BindNodeIdentity(ctx context.Context, v PublicNodeIdentity) error {
	_, err := s.pool.Exec(ctx, `INSERT INTO control.node_identity(singleton,node_id,public_key,key_fingerprint) VALUES(true,$1,$2,$3) ON CONFLICT(singleton) DO NOTHING`, v.NodeID, v.PublicKey, v.Fingerprint)
	if err != nil {
		return err
	}
	have, err := s.NodeIdentity(ctx)
	if err != nil {
		return err
	}
	if *have != v {
		return ErrNodeIdentityMismatch
	}
	return nil
}
func lockDirectory(ctx context.Context, tx pgx.Tx, org string) (int64, error) {
	_, err := tx.Exec(ctx, `INSERT INTO control.node_directories(organization_id) VALUES($1) ON CONFLICT DO NOTHING`, org)
	if err != nil {
		return 0, err
	}
	var rev int64
	err = tx.QueryRow(ctx, `SELECT revision FROM control.node_directories WHERE organization_id=$1 FOR UPDATE`, org).Scan(&rev)
	return rev, err
}
func bumpDirectory(ctx context.Context, tx pgx.Tx, org string, rev int64) error {
	_, err := tx.Exec(ctx, `UPDATE control.node_directories SET revision=$2 WHERE organization_id=$1`, org, rev)
	return err
}

func (s *Store) RegisterNode(ctx context.Context, org string, in discovery.Registration) (int64, error) {
	var revision int64
	err := s.InTx(ctx, func(tx pgx.Tx) error {
		rev, err := lockDirectory(ctx, tx, org)
		if err != nil {
			return err
		}
		var oldRev int64
		var oldState string
		err = tx.QueryRow(ctx, `SELECT descriptor_revision,state FROM control.node_members WHERE organization_id=$1 AND node_id=$2`, org, in.Descriptor.NodeID).Scan(&oldRev, &oldState)
		if err != nil && !errors.Is(err, pgx.ErrNoRows) {
			return err
		}
		if err == nil && (oldState == discovery.MemberRevoked || oldRev >= in.Descriptor.Revision) {
			return ErrDiscoveryConflict
		}
		// Membership validation is transactionally tied to the registration; arbitrary user IDs
		// cannot create a hidden sharing scope outside this organization.
		for _, subject := range in.AllowedSubjects {
			var exists bool
			err = tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM control.memberships m JOIN control.users u ON u.id=m.user_id WHERE m.organization_id=$1 AND m.user_id=$2 AND u.is_active)`, org, subject).Scan(&exists)
			if err != nil {
				return err
			}
			if !exists {
				return ErrNotFound
			}
		}
		body, err := json.Marshal(in.Descriptor)
		if err != nil {
			return err
		}
		revision = rev + 1
		subjects := in.AllowedSubjects
		if subjects == nil {
			subjects = []string{}
		}
		_, err = tx.Exec(ctx, `INSERT INTO control.node_members(organization_id,node_id,public_key,descriptor,descriptor_revision,state,visible_to_org,allowed_subjects,revision)
   VALUES($1,$2,$3,$4,$5,'pending',$6,$7,$8)
   ON CONFLICT(organization_id,node_id) DO UPDATE SET public_key=excluded.public_key,descriptor=excluded.descriptor,descriptor_revision=excluded.descriptor_revision,state='pending',visible_to_org=excluded.visible_to_org,allowed_subjects=excluded.allowed_subjects,revision=excluded.revision,public_revision=control.node_members.public_revision+1,updated_at=now()`, org, in.Descriptor.NodeID, in.PublicKey, body, in.Descriptor.Revision, in.VisibleToOrg, subjects, revision)
		if err != nil {
			return err
		}
		return bumpDirectory(ctx, tx, org, revision)
	})
	return revision, err
}
func (s *Store) SetNodeState(ctx context.Context, org, node, state string) (int64, error) {
	var revision int64
	if state != discovery.MemberApproved && state != discovery.MemberRevoked {
		return 0, ErrDiscoveryConflict
	}
	err := s.InTx(ctx, func(tx pgx.Tx) error {
		rev, err := lockDirectory(ctx, tx, org)
		if err != nil {
			return err
		}
		var current string
		var currentRev int64
		var raw []byte
		err = tx.QueryRow(ctx, `SELECT state,revision,descriptor FROM control.node_members WHERE organization_id=$1 AND node_id=$2`, org, node).Scan(&current, &currentRev, &raw)
		if err != nil {
			return norows(err)
		}
		if current == state {
			revision = currentRev
			return nil
		}
		if current == discovery.MemberRevoked || (state == discovery.MemberApproved && current != discovery.MemberPending) {
			return ErrDiscoveryConflict
		}
		var d discovery.NodeDescriptor
		if err = json.Unmarshal(raw, &d); err != nil {
			return err
		}
		if state == discovery.MemberApproved && !d.ValidUntil.After(time.Now()) {
			return ErrDiscoveryConflict
		}
		revision = rev + 1
		_, err = tx.Exec(ctx, `UPDATE control.node_members SET state=$3,revision=$4,public_revision=public_revision+1,updated_at=now() WHERE organization_id=$1 AND node_id=$2`, org, node, state, revision)
		if err != nil {
			return err
		}
		return bumpDirectory(ctx, tx, org, revision)
	})
	return revision, err
}
func (s *Store) ListNodes(ctx context.Context, org, localID string) ([]discovery.Member, error) {
	rows, err := s.pool.Query(ctx, `SELECT descriptor,state,public_revision FROM control.node_members WHERE organization_id=$1 ORDER BY node_id`, org)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []discovery.Member{}
	for rows.Next() {
		var raw []byte
		var state string
		var rev int64
		if err = rows.Scan(&raw, &state, &rev); err != nil {
			return nil, err
		}
		var d discovery.NodeDescriptor
		if err = json.Unmarshal(raw, &d); err != nil {
			return nil, err
		}
		out = append(out, discovery.DirectMember(d, state, rev, localID, time.Now().UTC()))
	}
	return out, rows.Err()
}

func (s *Store) CreateMemberSnapshot(ctx context.Context, org, subject, scope, localID string, admin bool, pageSize int, ttl time.Duration) (*discovery.Snapshot, error) {
	if pageSize < 1 || pageSize > 100 || ttl < 30*time.Second || ttl > time.Hour {
		return nil, ErrDiscoveryConflict
	}
	snap := &discovery.Snapshot{ID: auth.NewID(), AuthorityNodeID: localID, CallerScopeHash: scope}
	err := s.InTx(ctx, func(tx pgx.Tx) error {
		_, err := lockDirectory(ctx, tx, org)
		if err != nil {
			return err
		}
		// Both snapshot creation and expiry use the database clock.
		if err = tx.QueryRow(ctx, `SELECT clock_timestamp()`).Scan(&snap.CreatedAt); err != nil {
			return err
		}
		snap.ExpiresAt = snap.CreatedAt.Add(ttl)
		rows, err := tx.Query(ctx, `SELECT descriptor,state,public_revision FROM control.node_members WHERE organization_id=$1 AND state='approved' AND ($3 OR visible_to_org OR $2=ANY(allowed_subjects)) ORDER BY node_id`, org, subject, admin)
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
		// Publish only changes to this caller's visible view. The global mutation
		// counter and route observation timestamps would disclose hidden activity
		// or manufacture a change each time an identical snapshot is requested.
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
 fingerprint=excluded.fingerprint RETURNING revision`, org, scope, hex.EncodeToString(digest[:])).Scan(&snap.RegistryRevision)
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
		_, err = tx.Exec(ctx, `INSERT INTO control.member_snapshots(id,organization_id,authority_node_id,caller_scope_hash,registry_revision,created_at,expires_at,first_cursor,terminal_cursor) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)`, snap.ID, org, localID, scope, snap.RegistryRevision, snap.CreatedAt, snap.ExpiresAt, snap.FirstCursor, snap.TerminalCursor)
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
func (s *Store) MemberSnapshotPage(ctx context.Context, org, subject, scope, id, cursor string, admin bool) (*discovery.Page, error) {
	out := &discovery.Page{}
	// Scope filtering precedes expiry so another subject cannot discover that a snapshot existed.
	var expired bool
	err := s.pool.QueryRow(ctx, `SELECT id,authority_node_id,caller_scope_hash,registry_revision,created_at,expires_at,first_cursor,terminal_cursor,expires_at<=clock_timestamp() FROM control.member_snapshots WHERE id=$1 AND organization_id=$2 AND caller_scope_hash=$3`, id, org, scope).Scan(&out.ID, &out.AuthorityNodeID, &out.CallerScopeHash, &out.RegistryRevision, &out.CreatedAt, &out.ExpiresAt, &out.FirstCursor, &out.TerminalCursor, &expired)
	if err != nil {
		return nil, norows(err)
	}
	if expired {
		return nil, ErrSnapshotExpired
	}
	if cursor == "" {
		cursor = out.FirstCursor
	}
	var body []byte
	err = s.pool.QueryRow(ctx, `SELECT members,next_cursor FROM control.member_snapshot_pages WHERE snapshot_id=$1 AND cursor=$2`, id, cursor).Scan(&body, &out.NextCursor)
	if err != nil {
		return nil, norows(err)
	}
	if err = json.Unmarshal(body, &out.Members); err != nil {
		return nil, err
	}
	out.Complete = cursor == out.TerminalCursor
	// Revocations invalidate permission immediately while preserving the frozen member slot.
	for i := range out.Members {
		m := &out.Members[i]
		var state string
		var visible bool
		var authorizationRevision int64
		err = s.pool.QueryRow(ctx, `SELECT state,($4 OR visible_to_org OR $3=ANY(allowed_subjects)),public_revision FROM control.node_members WHERE organization_id=$1 AND node_id=$2`, org, m.NodeID, subject, admin).Scan(&state, &visible, &authorizationRevision)
		if err != nil && !errors.Is(err, pgx.ErrNoRows) {
			return nil, err
		}
		// Registration always returns an approved member to pending and requires
		// approval again. Its persistent, monotonic public_revision is consequently
		// an authorization epoch too: later approval cannot revive the old descriptor,
		// key or visibility grant, even if nobody read during the revoked interval.
		if err != nil || state != discovery.MemberApproved || !visible || authorizationRevision != m.Revision {
			m.State = discovery.MemberRevoked
			m.Descriptor = nil
			m.Route = nil
			m.Configured = false
			m.ExpansionState = discovery.ExpansionRevoked
		}
	}
	return out, nil
}

// Local descriptor revision changes with endpoints/config, independently of node identity.
func (s *Store) NodeDescriptorRevision(ctx context.Context, configHash string) (int64, error) {
	var rev int64
	err := s.pool.QueryRow(ctx, `UPDATE control.node_identity SET descriptor_revision=CASE WHEN descriptor_hash='' OR descriptor_hash=$1 THEN descriptor_revision ELSE descriptor_revision+1 END,descriptor_hash=$1 WHERE singleton RETURNING descriptor_revision`, configHash).Scan(&rev)
	return rev, norows(err)
}
