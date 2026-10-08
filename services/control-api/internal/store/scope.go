package store

import (
	"context"
	"encoding/json"
	"errors"
	"time"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/auth"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/contracts"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/discovery"
	"github.com/jackc/pgx/v5"
)

// RevokedScopeNodes returns the current local trust revocations independently
// of snapshot visibility. Fresh snapshots omit these members; old snapshots
// also label hidden or superseded approvals revoked, which is not a trust state.
func (s *Store) RevokedScopeNodes(ctx context.Context, org string) (map[string]bool, error) {
	rows, err := s.pool.Query(ctx, `SELECT node_id FROM control.node_members WHERE organization_id=$1 AND state='revoked'`, org)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	nodes := map[string]bool{}
	for rows.Next() {
		var nodeID string
		if err = rows.Scan(&nodeID); err != nil {
			return nil, err
		}
		nodes[nodeID] = true
	}
	return nodes, rows.Err()
}

// CreateScope consumes one fixed, authorized member snapshot and a server-observed
// local collection catalog. Directory writes and the freeze share the directory lock.
// It is the local-only entry point; CreateExpandedScope additionally consumes the
// authenticated remote directory expansion.
func (s *Store) CreateScope(ctx context.Context, org, subject, callerScope, localID, scopeID string, admin bool, opts discovery.ScopeOptions, catalog discovery.CollectionCatalog) (*discovery.ScopeEnvelope, error) {
	return s.CreateExpandedScope(ctx, org, subject, callerScope, localID, scopeID, admin, opts, catalog, discovery.RemoteExpansion{})
}

// CreateExpandedScope freezes the caller-scoped denominator, the local catalog and
// the observed remote directories. Remote targets/revisions/unknowns are
// server-observed input: a caller can never assert them.
func (s *Store) CreateExpandedScope(ctx context.Context, org, subject, callerScope, localID, scopeID string, admin bool, opts discovery.ScopeOptions, catalog discovery.CollectionCatalog, remote discovery.RemoteExpansion) (*discovery.ScopeEnvelope, error) {
	if opts.Validate() != nil || !opts.AllowsNode(localID) || scopeID == "" {
		return nil, ErrDiscoveryConflict
	}
	out := &discovery.ScopeEnvelope{ContentSnapshot: "not_frozen"}
	err := s.InTx(ctx, func(tx pgx.Tx) error {
		if _, err := lockDirectory(ctx, tx, org); err != nil {
			return err
		}
		var snap discovery.Snapshot
		var now time.Time
		err := tx.QueryRow(ctx, `SELECT id,authority_node_id,caller_scope_hash,registry_revision,created_at,expires_at,first_cursor,terminal_cursor,clock_timestamp() FROM control.member_snapshots WHERE id=$1 AND organization_id=$2 AND caller_scope_hash=$3`, opts.MemberSnapshotID, org, callerScope).Scan(&snap.ID, &snap.AuthorityNodeID, &snap.CallerScopeHash, &snap.RegistryRevision, &snap.CreatedAt, &snap.ExpiresAt, &snap.FirstCursor, &snap.TerminalCursor, &now)
		if err != nil {
			return norows(err)
		}
		if snap.AuthorityNodeID != localID {
			return ErrDiscoveryConflict
		}
		m := discovery.ScopeManifest{Schema: "ddp-scope-coverage/1#ScopeManifest", ScopeID: scopeID, CallerScopeHash: callerScope, CreatedAt: now, ValidUntil: now.Add(time.Duration(opts.TTLSeconds) * time.Second), ExpandedMembers: []discovery.TargetKey{}, UnexpandedSubtrees: []discovery.UnknownSubtree{}, RegistryRevisionVector: []discovery.DirectoryRevision{{NodeID: localID, RegistryRevision: snap.RegistryRevision, FetchedAt: snap.CreatedAt, DirectoryRef: "members", SnapshotRef: snap.ID}}}
		if snap.ExpiresAt.Before(m.ValidUntil) {
			m.ValidUntil = snap.ExpiresAt
		}
		if !remote.ValidUntil.IsZero() && remote.ValidUntil.Before(m.ValidUntil) {
			m.ValidUntil = remote.ValidUntil
		}
		unknown := func(node, reason string) {
			m.UnexpandedSubtrees = append(m.UnexpandedSubtrees, discovery.UnknownSubtree{NodeID: node, Reason: reason})
		}
		remaining := opts.MaxMembers
		seenCollections := map[string]bool{}
		// A catalog is valid input only if tied to this scope/caller/node. A partially
		// consumed, consistent catalog may still contribute its observed targets.
		catalogBound := catalog.ScopeID == scopeID && catalog.CallerScopeHash == callerScope && catalog.NodeID == localID && catalog.SnapshotID != "" && catalog.Revision >= 1 && !catalog.FetchedAt.IsZero() && !catalog.ValidUntil.IsZero()
		if catalogBound {
			m.RegistryRevisionVector = append(m.RegistryRevisionVector, discovery.DirectoryRevision{NodeID: localID, RegistryRevision: catalog.Revision, FetchedAt: catalog.FetchedAt, DirectoryRef: "collections", SnapshotRef: catalog.SnapshotID})
			if catalog.ValidUntil.Before(m.ValidUntil) {
				m.ValidUntil = catalog.ValidUntil
			}
			for _, collection := range catalog.Collections {
				if seenCollections[collection] {
					continue
				}
				seenCollections[collection] = true
				if remaining == 0 {
					unknown(localID, "budget_exhausted")
					break
				}
				if collection == "" {
					unknown(localID, "unknown")
					continue
				}
				m.ExpandedMembers = append(m.ExpandedMembers, discovery.TargetKey{OriginNodeID: localID, CollectionID: collection, Operation: opts.Operation})
				remaining--
			}
		}
		if !catalogBound || !catalog.Complete || !catalog.ValidUntil.After(now) {
			reason := catalog.FailureReason
			if reason != "timeout" && reason != "denied" && reason != "enumeration_unsupported" && reason != "budget_exhausted" {
				reason = "unknown"
			}
			unknown(localID, reason)
		}
		// Remote targets were observed through configured peer endpoints and already
		// consumed the same member budget. Their origin is never caller-supplied.
		if remote.TargetBudgetUsed < remaining {
			remaining -= remote.TargetBudgetUsed
		} else {
			remaining = 0
		}
		remoteSeen := map[string]bool{}
		for _, target := range remote.Targets {
			if target.OriginNodeID == "" || target.CollectionID == "" || target.OriginNodeID == localID {
				continue
			}
			key := target.OriginNodeID + "\x00" + target.CollectionID
			if remoteSeen[key] {
				continue
			}
			remoteSeen[key] = true
			m.ExpandedMembers = append(m.ExpandedMembers, target)
		}
		m.RegistryRevisionVector = append(m.RegistryRevisionVector, remote.Revisions...)
		m.ChildManifests = remote.Children
		m.NodeRoutes = remote.NodeRoutes
		m.UnexpandedSubtrees = append(m.UnexpandedSubtrees, remote.Unknowns...)
		// Follow the stored cursor chain all the way to its separate terminal page.
		// A missing page, cycle, incomplete terminal or expired snapshot is partial.
		cursor := snap.FirstCursor
		seen := map[string]bool{}
		for {
			if !snap.ExpiresAt.After(now) || cursor == "" || seen[cursor] {
				unknown(localID, "unknown")
				break
			}
			seen[cursor] = true
			var raw []byte
			var next *string
			err = tx.QueryRow(ctx, `SELECT members,next_cursor FROM control.member_snapshot_pages WHERE snapshot_id=$1 AND cursor=$2`, snap.ID, cursor).Scan(&raw, &next)
			if errors.Is(err, pgx.ErrNoRows) {
				unknown(localID, "unknown")
				break
			}
			if err != nil {
				return err
			}
			var members []discovery.Member
			if json.Unmarshal(raw, &members) != nil {
				return ErrDiscoveryConflict
			}
			if cursor == snap.TerminalCursor {
				if len(members) != 0 || next != nil {
					unknown(localID, "unknown")
				}
				break
			}
			for _, member := range members {
				if !opts.AllowsNode(member.NodeID) {
					// Boundary-excluded members stay visible as denied subtrees so a
					// narrowed scope reports partial instead of a sealed denominator.
					unknown(member.NodeID, "denied")
					continue
				}
				if remote.Handled[member.NodeID] {
					// Expanded nodes already consumed the target budget and recorded
					// their outcome.
					continue
				}
				if remaining == 0 {
					unknown(localID, "budget_exhausted")
					break
				}
				remaining--
				reason := "unknown"
				if member.Descriptor != nil && member.Descriptor.ValidUntil.After(now) && !member.Descriptor.DiscoveryCapabilities.EnumerateMembers {
					reason = "enumeration_unsupported"
				}
				unknown(member.NodeID, reason)
			}
			if next == nil {
				unknown(localID, "unknown")
				break
			}
			cursor = *next
		}
		if err = discovery.FinalizeScope(&m); err != nil {
			return err
		}
		out.Manifest = m
		out.TotalTargets = len(m.ExpandedMembers)
		out.Expired = !m.ValidUntil.After(now)
		out.EffectiveEnumerationState = m.EnumerationState
		if out.Expired {
			out.EffectiveEnumerationState = string(contracts.EnumerationStateExpired)
		}
		pageCount := (len(m.ExpandedMembers) + opts.PageSize - 1) / opts.PageSize
		cursors := make([]string, pageCount+1)
		for i := range cursors {
			cursors[i] = auth.NewID()
		}
		out.FirstCursor = cursors[0]
		out.TerminalCursor = cursors[pageCount]
		body, err := json.Marshal(m)
		if err != nil {
			return err
		}
		childBody := []byte("[]")
		if len(m.ChildManifests) > 0 {
			if childBody, err = json.Marshal(m.ChildManifests); err != nil {
				return err
			}
		}
		_, err = tx.Exec(ctx, `INSERT INTO control.scope_manifests(id,organization_id,caller_scope_hash,member_snapshot_id,manifest,manifest_digest,valid_until,first_cursor,terminal_cursor,total_targets,child_manifests) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)`, scopeID, org, callerScope, snap.ID, body, m.ManifestDigest, m.ValidUntil, out.FirstCursor, out.TerminalCursor, out.TotalTargets, childBody)
		if err != nil {
			return err
		}
		for origin, via := range remote.Sources {
			if origin == "" || origin == localID || via == "" {
				continue
			}
			if _, err = tx.Exec(ctx, `INSERT INTO control.scope_remote_sources(scope_id,origin_node_id,via_node_id) VALUES($1,$2,$3) ON CONFLICT DO NOTHING`, scopeID, origin, via); err != nil {
				return err
			}
		}
		if catalogBound {
			_, err = tx.Exec(ctx, `INSERT INTO control.scope_catalog_sources(scope_id,origin_node_id,snapshot_id,terminal_cursor) VALUES($1,$2,$3,$4)`, scopeID, localID, catalog.SnapshotID, catalog.TerminalCursor)
			if err != nil {
				return err
			}
		}
		for i, cursor := range cursors {
			targets := []discovery.TargetKey{}
			var next *string
			if i < pageCount {
				targets = m.ExpandedMembers[i*opts.PageSize : min((i+1)*opts.PageSize, len(m.ExpandedMembers))]
				next = &cursors[i+1]
			}
			body, err := json.Marshal(targets)
			if err != nil {
				return err
			}
			if _, err = tx.Exec(ctx, `INSERT INTO control.scope_target_pages(scope_id,cursor,next_cursor,targets) VALUES($1,$2,$3,$4)`, scopeID, cursor, next, body); err != nil {
				return err
			}
		}
		return nil
	})
	return out, err
}

func (s *Store) ScopeManifest(ctx context.Context, org, callerScope, id string) (*discovery.ScopeEnvelope, error) {
	out := &discovery.ScopeEnvelope{ContentSnapshot: "not_frozen"}
	var body []byte
	err := s.pool.QueryRow(ctx, `SELECT manifest,first_cursor,terminal_cursor,total_targets,valid_until<=clock_timestamp() FROM control.scope_manifests WHERE id=$1 AND organization_id=$2 AND caller_scope_hash=$3`, id, org, callerScope).Scan(&body, &out.FirstCursor, &out.TerminalCursor, &out.TotalTargets, &out.Expired)
	if err != nil {
		return nil, norows(err)
	}
	if err = json.Unmarshal(body, &out.Manifest); err != nil {
		return nil, err
	}
	out.EffectiveEnumerationState = out.Manifest.EnumerationState
	if out.Expired {
		out.EffectiveEnumerationState = string(contracts.EnumerationStateExpired)
	}
	return out, nil
}

type ScopeCatalogSource struct {
	NodeID, SnapshotID, TerminalCursor string
	Revoked                            bool
}

func (s *Store) ScopeCatalogSource(ctx context.Context, org, callerScope, id string) (*ScopeCatalogSource, error) {
	var out ScopeCatalogSource
	err := s.pool.QueryRow(ctx, `SELECT c.origin_node_id,c.snapshot_id,c.terminal_cursor,EXISTS(SELECT 1 FROM control.scope_catalog_revocations r WHERE r.scope_id=m.id) FROM control.scope_catalog_sources c JOIN control.scope_manifests m ON m.id=c.scope_id WHERE m.id=$1 AND m.organization_id=$2 AND m.caller_scope_hash=$3`, id, org, callerScope).Scan(&out.NodeID, &out.SnapshotID, &out.TerminalCursor, &out.Revoked)
	return &out, norows(err)
}

// ScopeHasStoredRevocation 报告该 scope 是否已有持久化的点名 revoke：
// handleScopeTargets 用它在"已有点名 revoke"的快照上跳过在线重验，
// 后来的成功目录响应不能复活已撤回的目标，也不该继续打扰 producer。
func (s *Store) ScopeHasStoredRevocation(ctx context.Context, org, callerScope, id string) (bool, error) {
	var one int
	err := s.pool.QueryRow(ctx, `SELECT 1 FROM control.scope_target_pages p JOIN control.scope_manifests m ON m.id=p.scope_id, jsonb_array_elements(p.targets) t WHERE m.id=$1 AND m.organization_id=$2 AND m.caller_scope_hash=$3 AND t.value->>'state'='revoked' LIMIT 1`, id, org, callerScope).Scan(&one)
	if errors.Is(err, pgx.ErrNoRows) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	return true, nil
}
func (s *Store) RevokeScopeCatalog(ctx context.Context, org, callerScope, id string) error {
	source, err := s.ScopeCatalogSource(ctx, org, callerScope, id)
	if err == nil {
		return s.RevokeScopeCollections(ctx, org, callerScope, id, source.NodeID, nil)
	}
	if !errors.Is(err, ErrNotFound) {
		return err
	}
	// No catalog source row means the scope froze without a bound catalog
	// (producer failure or empty legacy scope). There is no single origin to
	// scope the revocation to, so revoke every target as the legacy behavior
	// did via the all-origins flag below.
	return s.revokeScopeTargets(ctx, org, callerScope, id, "", nil, true)
}

// A withdrawn catalog stops use of that snapshot; only specifically identified
// collections are labelled revoked. Other targets remain unknown/incomplete.
// nil means a trusted producer has revoked the entire catalog of that origin.
func (s *Store) RevokeScopeCollections(ctx context.Context, org, callerScope, id, originNodeID string, revokedIDs []string) error {
	if originNodeID == "" {
		return ErrDiscoveryConflict
	}
	return s.revokeScopeTargets(ctx, org, callerScope, id, originNodeID, revokedIDs, false)
}

func (s *Store) revokeScopeTargets(ctx context.Context, org, callerScope, id, originNodeID string, revokedIDs []string, allOrigins bool) error {
	all := revokedIDs == nil
	if revokedIDs == nil {
		revokedIDs = []string{}
	}
	// A bound 410 names only the withdrawn collections, but the whole snapshot
	// is invalid: unrevoked same-origin targets cannot be revalidated against
	// it, so the catalog row marks them unreachable on read while named ones
	// stay revoked (see ScopeTargets). Only the legacy all-origins path (scopes
	// frozen without a bound catalog) skips the row: there is no single origin
	// whose snapshot died.
	wholeCatalog := !allOrigins
	return s.InTx(ctx, func(tx pgx.Tx) error {
		if wholeCatalog {
			_, err := tx.Exec(ctx, `INSERT INTO control.scope_catalog_revocations(scope_id) SELECT id FROM control.scope_manifests WHERE id=$1 AND organization_id=$2 AND caller_scope_hash=$3 ON CONFLICT DO NOTHING`, id, org, callerScope)
			if err != nil {
				return err
			}
		}
		_, err := tx.Exec(ctx, `UPDATE control.scope_target_pages p SET targets=(SELECT COALESCE(jsonb_agg(CASE WHEN $5 OR (t.value->>'origin_node_id'=$6 AND ($7 OR t.value->>'collection_id'=ANY($4::text[]))) THEN t.value || '{"state":"revoked"}'::jsonb ELSE t.value END ORDER BY t.ord),'[]'::jsonb) FROM jsonb_array_elements(p.targets) WITH ORDINALITY AS t(value,ord)) FROM control.scope_manifests m WHERE p.scope_id=m.id AND m.id=$1 AND m.organization_id=$2 AND m.caller_scope_hash=$3`, id, org, callerScope, revokedIDs, allOrigins, originNodeID, all)
		return err
	})
}

func (s *Store) ScopeTargets(ctx context.Context, org, subject, callerScope, id, cursor, localID string, admin bool) (*discovery.ScopeTargetPage, error) {
	envelope, err := s.ScopeManifest(ctx, org, callerScope, id)
	if err != nil {
		return nil, err
	}
	if cursor == "" {
		cursor = envelope.FirstCursor
	}
	out := &discovery.ScopeTargetPage{ScopeID: id, ManifestDigest: envelope.Manifest.ManifestDigest, Targets: []discovery.ScopeTarget{}, TotalTargets: envelope.TotalTargets, Expired: envelope.Expired}
	var body []byte
	err = s.pool.QueryRow(ctx, `SELECT targets,next_cursor FROM control.scope_target_pages WHERE scope_id=$1 AND cursor=$2`, id, cursor).Scan(&body, &out.NextCursor)
	if err != nil {
		return nil, norows(err)
	}
	var keys []struct {
		discovery.TargetKey
		State string `json:"state,omitempty"`
	}
	if err = json.Unmarshal(body, &keys); err != nil {
		return nil, err
	}
	out.Complete = cursor == envelope.TerminalCursor
	source, sourceErr := s.ScopeCatalogSource(ctx, org, callerScope, id)
	if sourceErr != nil && !errors.Is(sourceErr, ErrNotFound) {
		return nil, sourceErr
	}
	for _, key := range keys {
		state := string(contracts.CoverageTargetStateNotAttempted)
		if key.State == string(contracts.CoverageTargetStateRevoked) {
			state = key.State
		} else if sourceErr == nil && source.Revoked && key.OriginNodeID == source.NodeID {
			// catalog 整体撤回后：没被点名的同源目标只是"目录不可用"，
			// 不是 revocation。点名 revoke 的上面已经处理。
			state = string(contracts.CoverageTargetStateUnreachable)
		}
		if key.OriginNodeID != localID {
			// A discovered origin (B through P) has no local registration of its
			// own. Its authorization root is the approved direct member through
			// which the expansion reached it; revoking that root revokes the
			// discovered targets instead of treating an unregistered node as one.
			var authorized bool
			err = s.pool.QueryRow(ctx, `SELECT EXISTS(
    SELECT 1 FROM control.node_members m
    WHERE m.organization_id=$1 AND m.node_id=$2 AND m.state='approved' AND ($4 OR m.visible_to_org OR $3=ANY(m.allowed_subjects))
) OR EXISTS(
    SELECT 1 FROM control.scope_remote_sources s
    JOIN control.node_members m ON m.organization_id=$1 AND m.node_id=s.via_node_id
    WHERE s.scope_id=$5 AND s.origin_node_id=$2 AND m.state='approved' AND ($4 OR m.visible_to_org OR $3=ANY(m.allowed_subjects))
)`, org, key.OriginNodeID, subject, admin, id).Scan(&authorized)
			if err != nil {
				return nil, err
			}
			if !authorized {
				state = string(contracts.CoverageTargetStateRevoked)
			}
		}
		out.Targets = append(out.Targets, discovery.ScopeTarget{TargetKey: key.TargetKey, State: state})
	}
	return out, nil
}
