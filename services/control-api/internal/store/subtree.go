package store

import (
	"context"
	"encoding/json"
	"time"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/auth"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/discovery"
	"github.com/jackc/pgx/v5"
)

// Retain at most 32 snapshots per approved issuer, and expire them after five
// minutes. A directory lock serializes creation and eviction across replicas.
func (s *Store) CreateSubtreeSnapshot(ctx context.Context, org, issuer, binding string, pageSize int, page discovery.SubtreePage, targets []discovery.RoutedTarget) (*discovery.SubtreePage, error) {
	if pageSize < 1 || pageSize > 100 || len(targets) > 10000 {
		return nil, ErrDiscoveryConflict
	}
	err := s.InTx(ctx, func(tx pgx.Tx) error {
		if _, err := lockDirectory(ctx, tx, org); err != nil {
			return err
		}
		if _, err := tx.Exec(ctx, `DELETE FROM control.subtree_snapshots WHERE expires_at<=clock_timestamp()`); err != nil {
			return err
		}
		if _, err := tx.Exec(ctx, `DELETE FROM control.subtree_snapshots WHERE id IN (SELECT id FROM control.subtree_snapshots WHERE organization_id=$1 AND issuer_node_id=$2 ORDER BY created_at DESC,id DESC OFFSET 31)`, org, issuer); err != nil {
			return err
		}
		page.SnapshotID = auth.NewID()
		if err := tx.QueryRow(ctx, `SELECT clock_timestamp()`).Scan(&page.CreatedAt); err != nil {
			return err
		}
		until := page.CreatedAt.Add(5 * time.Minute)
		if page.ValidUntil.IsZero() || until.Before(page.ValidUntil) {
			page.ValidUntil = until
		}
		count := (len(targets) + pageSize - 1) / pageSize
		cursors := make([]string, count+1)
		for i := range cursors {
			cursors[i] = auth.NewID()
		}
		page.FirstCursor = cursors[0]
		page.TerminalCursor = cursors[count]
		page.Targets = []discovery.RoutedTarget{}
		body, err := json.Marshal(page)
		if err != nil {
			return err
		}
		if len(body) > 8<<20 {
			return ErrDiscoveryConflict
		}
		if _, err = tx.Exec(ctx, `INSERT INTO control.subtree_snapshots(id,organization_id,issuer_node_id,request_binding,page_size,created_at,expires_at,metadata) VALUES($1,$2,$3,$4,$5,$6,$7,$8)`, page.SnapshotID, org, issuer, binding, pageSize, page.CreatedAt, page.ValidUntil, body); err != nil {
			return err
		}
		for i, cursor := range cursors {
			items := []discovery.RoutedTarget{}
			var next *string
			if i < count {
				items = targets[i*pageSize : min((i+1)*pageSize, len(targets))]
				next = &cursors[i+1]
			}
			body, err = json.Marshal(items)
			if err != nil {
				return err
			}
			if _, err = tx.Exec(ctx, `INSERT INTO control.subtree_snapshot_pages(snapshot_id,cursor,targets,next_cursor) VALUES($1,$2,$3,$4)`, page.SnapshotID, cursor, body, next); err != nil {
				return err
			}
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	return s.SubtreeSnapshotPage(ctx, org, issuer, binding, page.SnapshotID, "", 0)
}

func (s *Store) SubtreeSnapshotPage(ctx context.Context, org, issuer, binding, id, cursor string, limit int) (*discovery.SubtreePage, error) {
	var metadata []byte
	var pageSize int
	var expired bool
	err := s.pool.QueryRow(ctx, `SELECT metadata,page_size,expires_at<=clock_timestamp() FROM control.subtree_snapshots WHERE id=$1 AND organization_id=$2 AND issuer_node_id=$3 AND request_binding=$4`, id, org, issuer, binding).Scan(&metadata, &pageSize, &expired)
	if err != nil {
		return nil, norows(err)
	}
	if expired {
		return nil, ErrSnapshotExpired
	}
	if limit != 0 && limit != pageSize {
		return nil, ErrDiscoveryConflict
	}
	var out discovery.SubtreePage
	if err = json.Unmarshal(metadata, &out); err != nil {
		return nil, err
	}
	if cursor == "" {
		cursor = out.FirstCursor
	}
	var targets []byte
	if err = s.pool.QueryRow(ctx, `SELECT targets,next_cursor FROM control.subtree_snapshot_pages WHERE snapshot_id=$1 AND cursor=$2`, id, cursor).Scan(&targets, &out.NextCursor); err != nil {
		return nil, norows(err)
	}
	if err = json.Unmarshal(targets, &out.Targets); err != nil {
		return nil, err
	}
	out.Complete = cursor == out.TerminalCursor
	return &out, nil
}
