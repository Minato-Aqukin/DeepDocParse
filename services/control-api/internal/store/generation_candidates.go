package store

import (
	"context"
	"encoding/json"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/discovery"
)

// GenerationMembers reads the live caller-visible approval view, not an old
// snapshot. Expiry uses the database clock, as approval and snapshot reads do.
func (s *Store) GenerationMembers(ctx context.Context, org, subject, localID string, admin bool) ([]discovery.NodeDescriptor, error) {
	rows, err := s.pool.Query(ctx, `SELECT descriptor FROM control.node_members
WHERE organization_id=$1 AND state='approved' AND node_id<>$3
AND ($4 OR visible_to_org OR $2=ANY(allowed_subjects))
AND (descriptor->>'valid_until')::timestamptz>clock_timestamp() ORDER BY node_id`, org, subject, localID, admin)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []discovery.NodeDescriptor{}
	for rows.Next() {
		var raw []byte
		if err := rows.Scan(&raw); err != nil {
			return nil, err
		}
		var descriptor discovery.NodeDescriptor
		if err := json.Unmarshal(raw, &descriptor); err != nil {
			return nil, err
		}
		out = append(out, descriptor)
	}
	return out, rows.Err()
}
