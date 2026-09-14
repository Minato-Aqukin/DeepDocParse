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
	if opts.Validate() != nil || scopeID == "" {
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
				if remaining == 0 {
					unknown(localID, "budget_exhausted")
					break
				}
				remaining--
				if remote.Handled[member.NodeID] {
					// The expansion already recorded this member's outcome: either it
					// contributed observed remote directories or it has an explicit
					// unexpanded reason. Never invent a second, different one.
					continue
				}
				// Remote catalog credentials/collection enumeration have not been
				// implemented. Membership is retained as an explicit unknown subtree.
				reason := "unknown"
				if member.Descriptor != nil && member.Descriptor.ValidUntil.After(now) && !member.Descriptor.DiscoveryCapabilities.EnumerateMembers {
					reason = "enumeration_unsupported"
				}
				unknown(member.NodeID, reason)
			}
			if remaining == 0 && (len(members) > 0 || next != nil) {
				unknown(localID, "budget_exhausted")
				break
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

func (s *Store) RevokeScopeCatalog(ctx context.Context, org, callerScope, id string) error {
	return s.RevokeScopeCollections(ctx, org, callerScope, id, nil)
}

// A withdrawn catalog stops use of that snapshot; only specifically identified
// collections are labelled revoked. Other targets remain unknown/incomplete.
// nil means a trusted producer has revoked the entire catalog.
func (s *Store) RevokeScopeCollections(ctx context.Context, org, callerScope, id string, revokedIDs []string) error {
	all := revokedIDs == nil
	if revokedIDs == nil {
		revokedIDs = []string{}
	}
	return s.InTx(ctx, func(tx pgx.Tx) error {
		_, err := tx.Exec(ctx, `INSERT INTO control.scope_catalog_revocations(scope_id) SELECT id FROM control.scope_manifests WHERE id=$1 AND organization_id=$2 AND caller_scope_hash=$3 ON CONFLICT DO NOTHING`, id, org, callerScope)
		if err != nil {
			return err
		}
		_, err = tx.Exec(ctx, `UPDATE control.scope_target_pages p SET targets=(SELECT COALESCE(jsonb_agg(CASE WHEN $5 OR t.value->>'collection_id'=ANY($4::text[]) THEN t.value || '{"state":"revoked"}'::jsonb ELSE t.value END ORDER BY t.ord),'[]'::jsonb) FROM jsonb_array_elements(p.targets) WITH ORDINALITY AS t(value,ord)) FROM control.scope_manifests m WHERE p.scope_id=m.id AND m.id=$1 AND m.organization_id=$2 AND m.caller_scope_hash=$3`, id, org, callerScope, revokedIDs, all)
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
		if sourceErr == nil && source.Revoked && key.OriginNodeID == source.NodeID {
			state = string(contracts.CoverageTargetStateUnreachable)
		}
		if key.State == string(contracts.CoverageTargetStateRevoked) {
			state = key.State
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
