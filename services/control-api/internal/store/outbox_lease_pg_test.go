package store

import (
	"context"
	"encoding/json"
	"testing"

	"github.com/jackc/pgx/v5"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/contracts"
)

// enqueueTestEvent inserts one claimable outbox row inside a caller tx,
// mirroring how production code calls EnqueueOutbox from uploads.go.
func enqueueTestEvent(t *testing.T, s *Store, org, typ string) string {
	t.Helper()
	var id string
	err := s.InTx(context.Background(), func(tx pgx.Tx) error {
		payload, _ := json.Marshal(map[string]any{"upload_id": "u-" + org[:4]})
		if err := EnqueueOutbox(context.Background(), tx, org, typ, payload); err != nil {
			return err
		}
		return tx.QueryRow(context.Background(), `SELECT id FROM control.control_outbox WHERE organization_id=$1 ORDER BY created_at DESC LIMIT 1`, org).Scan(&id)
	})
	if err != nil {
		t.Fatal(err)
	}
	return id
}

// Claim sets a re-claim lease — immediate second claim must not
// return the same in-flight event; MarkOutboxFailed re-arms it with backoff.
func TestClaimOutboxLeaseNoImmediateReclaim(t *testing.T) {
	s, org := handoffStore(t)
	ctx := context.Background()
	id := enqueueTestEvent(t, s, org, "DocumentSubmitted")
	// 领取是全局队列（生产投递器不分组织）：一次只领一批，
	// 脏库里的旧事件可能排在前面 —— 循环领直到自己这一行出现。
	var first []OutboxEvent
	for range 64 {
		batch, err := s.ClaimOutbox(ctx, 8)
		if err != nil {
			t.Fatal(err)
		}
		if len(batch) == 0 {
			break
		}
		for _, e := range batch {
			if e.ID == id {
				first = batch
			}
		}
		if first != nil {
			break
		}
	}
	if first == nil {
		t.Fatalf("test event never claimed")
	}
	second, err := s.ClaimOutbox(ctx, 8)
	if err != nil {
		t.Fatal(err)
	}
	for _, e := range second {
		if e.ID == id {
			t.Fatalf("in-flight event re-claimed without lease expiry")
		}
	}
	// Failure re-arms with backoff: still not immediately claimable.
	if err := s.MarkOutboxFailed(ctx, id, first[0].Attempts, "boom"); err != nil {
		t.Fatal(err)
	}
	third, err := s.ClaimOutbox(ctx, 8)
	if err != nil {
		t.Fatal(err)
	}
	for _, e := range third {
		if e.ID == id {
			t.Fatalf("failed event re-claimed before backoff elapsed")
		}
	}
	if err := s.MarkOutboxDelivered(ctx, id); err != nil {
		t.Fatal(err)
	}
	_ = contracts.IngestRejection("invalid_upload_target")
}
