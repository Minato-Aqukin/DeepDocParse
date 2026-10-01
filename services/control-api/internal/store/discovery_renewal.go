package store

import (
	"context"
	"encoding/json"
	"time"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/discovery"
	"github.com/jackc/pgx/v5"
)

type DiscoveryRenewal struct {
	OrganizationID string
	PublicKey      string
	Descriptor     discovery.NodeDescriptor
	Attempts       int
	ClaimedAt      time.Time
	raw            []byte
}

// The claim is a crash-recoverable minute lease: 32 peers / 4 workers * 2s
// is at most 16s of network work. Another replica cannot claim the same row.
func (s *Store) ClaimDiscoveryRenewals(ctx context.Context) ([]DiscoveryRenewal, error) {
	rows, err := s.pool.Query(ctx, `WITH due AS (
 SELECT organization_id,node_id FROM control.node_members
 WHERE state='approved' AND renewal_next_attempt_at<=now()
 ORDER BY renewal_next_attempt_at,organization_id,node_id LIMIT 32 FOR UPDATE SKIP LOCKED
 ) UPDATE control.node_members m SET renewal_last_attempt_at=now(),renewal_next_attempt_at=now()+interval '1 minute'
 FROM due WHERE m.organization_id=due.organization_id AND m.node_id=due.node_id
 RETURNING m.organization_id,m.public_key,m.descriptor,m.renewal_attempts,m.renewal_last_attempt_at`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []DiscoveryRenewal{}
	for rows.Next() {
		var claim DiscoveryRenewal
		if err := rows.Scan(&claim.OrganizationID, &claim.PublicKey, &claim.raw, &claim.Attempts, &claim.ClaimedAt); err != nil {
			return nil, err
		}
		if err := json.Unmarshal(claim.raw, &claim.Descriptor); err != nil {
			return nil, err
		}
		out = append(out, claim)
	}
	return out, rows.Err()
}

// CompleteDiscoveryRenewal rejects a stale observation if approval, configuration
// or the claim changed while the network request was in flight.
func (s *Store) CompleteDiscoveryRenewal(ctx context.Context, claim DiscoveryRenewal, d discovery.NodeDescriptor, failure error, interval time.Duration) (bool, error) {
	applied := false
	err := s.InTx(ctx, func(tx pgx.Tx) error {
		rev, err := lockDirectory(ctx, tx, claim.OrganizationID)
		if err != nil {
			return err
		}
		if failure != nil {
			delay := interval
			for n := 0; n <= claim.Attempts && delay < 120*time.Second; n++ {
				delay *= 2
			}
			if delay > 120*time.Second {
				delay = 120 * time.Second
			}
			result, err := tx.Exec(ctx, `UPDATE control.node_members SET renewal_attempts=renewal_attempts+1,
    renewal_last_error=$6,renewal_next_attempt_at=now()+$7*interval '1 second'
    WHERE organization_id=$1 AND node_id=$2 AND state='approved' AND public_key=$3
    AND descriptor=$4::jsonb AND renewal_last_attempt_at=$5`, claim.OrganizationID, claim.Descriptor.NodeID, claim.PublicKey, claim.raw, claim.ClaimedAt, failure.Error(), delay.Seconds())
			if err != nil {
				return err
			}
			applied = result.RowsAffected() == 1
			return nil
		}
		if !discovery.SameDescriptorConfiguration(claim.Descriptor, d) || !d.ValidUntil.After(claim.Descriptor.ValidUntil) {
			return ErrDiscoveryConflict
		}
		if err := d.ValidateLease(claim.PublicKey, "", time.Now()); err != nil {
			return err
		}
		body, err := json.Marshal(d)
		if err != nil {
			return err
		}
		result, err := tx.Exec(ctx, `UPDATE control.node_members SET descriptor=$6,revision=$7,updated_at=now(),
   renewal_attempts=0,renewal_last_error=NULL,renewal_last_success_at=now(),renewal_next_attempt_at=now()+$8*interval '1 second'
   WHERE organization_id=$1 AND node_id=$2 AND state='approved' AND public_key=$3
   AND descriptor=$4::jsonb AND renewal_last_attempt_at=$5`, claim.OrganizationID, claim.Descriptor.NodeID, claim.PublicKey, claim.raw, claim.ClaimedAt, body, rev+1, interval.Seconds())
		if err != nil {
			return err
		}
		applied = result.RowsAffected() == 1
		if !applied {
			return nil
		}
		return bumpDirectory(ctx, tx, claim.OrganizationID, rev+1)
	})
	return applied, err
}
