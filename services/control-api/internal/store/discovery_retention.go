package store

import (
	"context"
	"time"

	"github.com/jackc/pgx/v5"
)

// DiscoveryRetentionStats counts parent rows; dependent pages/source rows cascade.
// Task-owned frozen manifests remain in corpus, not in this rebuildable directory cache.
type DiscoveryRetentionStats struct {
	Scopes   int64
	Members  int64
	Subtrees int64
}

// SweepDiscoveryMetadata removes expired directory projections only after their
// configured retention window. Scopes go first because they reference member
// snapshots; still-referenced snapshots are never deleted independently.
func (s *Store) SweepDiscoveryMetadata(ctx context.Context, retention time.Duration, limit int) (DiscoveryRetentionStats, error) {
	var stats DiscoveryRetentionStats
	if retention < 0 || limit < 1 || limit > 5000 {
		return stats, ErrDiscoveryConflict
	}
	err := s.InTx(ctx, func(tx pgx.Tx) error {
		tag, err := tx.Exec(ctx, `DELETE FROM control.scope_manifests WHERE id IN (
 SELECT id FROM control.scope_manifests
 WHERE valid_until < clock_timestamp()-$1*interval '1 second'
 ORDER BY valid_until,id LIMIT $2 FOR UPDATE SKIP LOCKED
)`, retention.Seconds(), limit)
		if err != nil {
			return err
		}
		stats.Scopes = tag.RowsAffected()
		tag, err = tx.Exec(ctx, `DELETE FROM control.member_snapshots WHERE id IN (
 SELECT m.id FROM control.member_snapshots m
 WHERE m.expires_at < clock_timestamp()-$1*interval '1 second'
 AND NOT EXISTS (SELECT 1 FROM control.scope_manifests s WHERE s.member_snapshot_id=m.id)
 ORDER BY m.expires_at,m.id LIMIT $2 FOR UPDATE OF m SKIP LOCKED
)`, retention.Seconds(), limit)
		if err != nil {
			return err
		}
		stats.Members = tag.RowsAffected()
		tag, err = tx.Exec(ctx, `DELETE FROM control.subtree_snapshots WHERE id IN (
 SELECT id FROM control.subtree_snapshots
 WHERE expires_at < clock_timestamp()-$1*interval '1 second'
 ORDER BY expires_at,id LIMIT $2 FOR UPDATE SKIP LOCKED
)`, retention.Seconds(), limit)
		if err != nil {
			return err
		}
		stats.Subtrees = tag.RowsAffected()
		return nil
	})
	if err != nil {
		return DiscoveryRetentionStats{}, err
	}
	return stats, nil
}
