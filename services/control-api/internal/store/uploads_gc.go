package store

import (
	"context"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"
)

// UploadReclamation holds a terminal session's immutable original-key claim.
type UploadReclamation struct {
	ID             string
	OrganizationID string
	ObjectKey      string
	EligibleAt     time.Time
}

// ReclaimTerminalUploads commits a five-minute lease before destructive cleanup.
// Finalization is fenced by that lease; failed callbacks retain a bounded diagnostic.
func (s *Store) ReclaimTerminalUploads(ctx context.Context, grace time.Duration, limit int, reclaim func(UploadReclamation) (bool, error)) (int, error) {
	cleaned := 0
	for range limit {
		claimed := false
		var u UploadReclamation
		var claimedAt time.Time
		err := s.InTx(ctx, func(tx pgx.Tx) error {
			// Keep the row lock only until the lease timestamp is persisted.
			err := tx.QueryRow(ctx, `
    SELECT u.id,u.organization_id,u.object_key,
      greatest(u.expires_at,u.updated_at,coalesce(e.rejected_at,u.updated_at))
    FROM control.upload_sessions u
    LEFT JOIN LATERAL (
      SELECT rejected_at FROM control.control_outbox
      WHERE organization_id=u.organization_id AND type='DocumentSubmitted'
        AND payload->>'upload_id'=u.id
      ORDER BY created_at DESC LIMIT 1
    ) e ON true
    WHERE u.reclaimed_at IS NULL AND u.allocation_state IN ('pending','ready')
      AND (u.status IN ('failed','expired')
        OR (u.status='ready' AND u.purpose='permanent' AND e.rejected_at IS NOT NULL))
      AND greatest(u.expires_at,u.updated_at,coalesce(e.rejected_at,u.updated_at)) < now()-($1 * interval '1 second')
      AND NOT EXISTS (
       SELECT 1 FROM control.upload_sessions other
       WHERE other.id<>u.id AND other.object_key=u.object_key AND other.reclaimed_at IS NULL)
      AND (u.reclaim_attempted_at IS NULL OR u.reclaim_attempted_at < now()-interval '5 minutes')
    ORDER BY u.updated_at
    FOR UPDATE OF u SKIP LOCKED LIMIT 1`, grace.Seconds()).
				Scan(&u.ID, &u.OrganizationID, &u.ObjectKey, &u.EligibleAt)
			if err == pgx.ErrNoRows {
				return nil
			}
			if err != nil {
				return err
			}
			err = tx.QueryRow(ctx, `UPDATE control.upload_sessions SET reclaim_attempted_at=now() WHERE id=$1 RETURNING reclaim_attempted_at`, u.ID).Scan(&claimedAt)
			claimed = err == nil
			return err
		})
		if err != nil {
			return cleaned, err
		}
		if !claimed {
			break
		}
		// No transaction or pooled connection is held during HTTP/S3 work.
		ok, failure := reclaim(u)
		var diagnostic any
		if failure != nil {
			diagnostic = fmt.Sprintf("reclaim_failed:%T:%.200s", failure, failure.Error())
		} else if !ok {
			diagnostic = "reference_protected"
		}
		finalized := false
		err = s.InTx(ctx, func(tx pgx.Tx) error {
			if ok && failure == nil {
				tag, err := tx.Exec(ctx, `UPDATE control.upload_sessions
 SET reclaimed_at=now(),reclaim_error=NULL,reclaim_attempted_at=now()
 WHERE id=$1 AND reclaimed_at IS NULL AND reclaim_attempted_at=$2`, u.ID, claimedAt)
				finalized = tag.RowsAffected() == 1
				return err
			}
			_, err := tx.Exec(ctx, `UPDATE control.upload_sessions
 SET reclaim_error=$3,reclaim_attempted_at=now()
 WHERE id=$1 AND reclaimed_at IS NULL AND reclaim_attempted_at=$2`, u.ID, claimedAt, diagnostic)
			return err
		})
		if err != nil {
			return cleaned, err
		}
		if finalized {
			cleaned++
		}
	}
	return cleaned, nil
}
