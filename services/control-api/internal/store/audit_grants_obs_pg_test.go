package store

import (
	"context"
	"encoding/json"
	"testing"
	"time"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/obs"
)

// Federation correlation lands in audit detail with stable key names,
// and never carries secrets.
func TestAuditFederationCarriesCorrelationTuple(t *testing.T) {
	s, org := handoffStore(t)
	ctx := context.Background()
	root, step, probe, adm, cov, del := "root-1", "step-2", "probe-3", "adm-4", "cov-5", "delivered"
	attempt := 3
	s.AuditFederation(ctx, org, "actor-1", "user", "federation.probe_created", "probe-3", "req-1",
		map[string]any{"note": "hello"}, FederationCorrelation{
			RootTaskID: &root, StepID: &step, ProbeID: &probe, AdmissionID: &adm,
			Attempt: &attempt, CoverageRef: &cov, DeliveryState: &del,
		})
	events, err := s.AuditEvents(ctx, org, "federation.probe_created", nil, 5)
	if err != nil || len(events) == 0 {
		t.Fatalf("audit missing: %+v %v", events, err)
	}
	var detail map[string]any
	if err := json.Unmarshal(events[0].Detail, &detail); err != nil {
		t.Fatal(err)
	}
	for k, want := range map[string]any{
		"root_task_id": "root-1", "step_id": "step-2", "probe_id": "probe-3",
		"admission_id": "adm-4", "coverage_ref": "cov-5", "delivery_state": "delivered",
	} {
		if detail[k] != want {
			t.Fatalf("detail[%q]=%v want %v (full %v)", k, detail[k], want, detail)
		}
	}
	if detail["attempt"] != float64(3) {
		t.Fatalf("attempt not carried: %v", detail)
	}
	// Explicit caller value wins over the tuple.
	s.AuditFederation(ctx, org, "actor-1", "user", "federation.probe_created", "probe-3", "req-1",
		map[string]any{"step_id": "caller-wins"}, FederationCorrelation{StepID: &step})
	events, err = s.AuditEvents(ctx, org, "federation.probe_created", nil, 5)
	if err != nil || len(events) == 0 {
		t.Fatalf("audit missing: %+v %v", events, err)
	}
	var d2 map[string]any
	if err := json.Unmarshal(events[0].Detail, &d2); err != nil {
		t.Fatal(err)
	}
	if d2["step_id"] != "caller-wins" {
		t.Fatalf("caller value overwritten: %v", d2)
	}
}

// Stable grants are bounded — new rows carry expires_at ~24h,
// expired tokens are rejected, and re-issue mints a fresh usable token.
func TestStableGrantBoundedLifetime(t *testing.T) {
	s, org := handoffStore(t)
	ctx := context.Background()
	g, err := s.StableGrantFor(ctx, org, "doc-19", "alice", "res", "obj", "application/pdf", "a.pdf")
	if err != nil {
		t.Fatal(err)
	}
	if g.ExpiresAt == nil {
		t.Fatalf("new grant has no expiry")
	}
	ttl := time.Until(*g.ExpiresAt)
	if ttl < 23*time.Hour || ttl > 25*time.Hour {
		t.Fatalf("grant ttl=%v, want ~24h", ttl)
	}
	if _, err := s.FileGrantByToken(ctx, g.Token); err != nil {
		t.Fatalf("fresh grant must redeem: %v", err)
	}
	if _, err := s.pool.Exec(ctx, `UPDATE control.file_grants SET expires_at=now()-interval '1 second' WHERE token=$1`, g.Token); err != nil {
		t.Fatal(err)
	}
	if _, err := s.FileGrantByToken(ctx, g.Token); err == nil {
		t.Fatalf("expired grant must not redeem")
	}
	fresh, err := s.StableGrantFor(ctx, org, "doc-19", "alice", "res", "obj", "application/pdf", "a.pdf")
	if err != nil {
		t.Fatal(err)
	}
	if fresh.Token == g.Token {
		t.Fatalf("re-issue after expiry must mint a new token")
	}
	if _, err := s.FileGrantByToken(ctx, fresh.Token); err != nil {
		t.Fatalf("re-issued grant must redeem: %v", err)
	}
}

// 绕过 Go 路径的手工行（expires_at NULL）不得兑换：0017 之后新行必有
// expires_at，存量 NULL 行已被回填 —— 残留的 NULL 只能是手工行，
// FileGrantByToken 按"未知寿命 = 无效"拒掉，而不是当成永不过期。
func TestFileGrantByTokenRejectsNullExpiry(t *testing.T) {
	s, org := handoffStore(t)
	ctx := context.Background()
	g, err := s.StableGrantFor(ctx, org, "doc-null-exp", "alice", "res", "obj", "application/pdf", "a.pdf")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := s.pool.Exec(ctx, `UPDATE control.file_grants SET expires_at=NULL WHERE token=$1`, g.Token); err != nil {
		t.Fatal(err)
	}
	if _, err := s.FileGrantByToken(ctx, g.Token); err == nil {
		t.Fatalf("NULL-expiry grant must not redeem")
	}
}

// A grant that is still valid but close to expiry is not handed out again:
// the gateway fetches it only after queueing, so a few remaining minutes would
// fail the parse. Renewal mints a new token and revokes the superseded one.
func TestStableGrantRenewsBeforeExpiryWindow(t *testing.T) {
	s, org := handoffStore(t)
	ctx := context.Background()
	g, err := s.StableGrantFor(ctx, org, "doc-20", "alice", "res", "obj", "application/pdf", "a.pdf")
	if err != nil {
		t.Fatal(err)
	}
	again, err := s.StableGrantFor(ctx, org, "doc-20", "alice", "res", "obj", "application/pdf", "a.pdf")
	if err != nil {
		t.Fatal(err)
	}
	if again.Token != g.Token {
		t.Fatalf("a long-lived grant must be reused for idempotent fetches")
	}
	if _, err := s.pool.Exec(ctx, `UPDATE control.file_grants SET expires_at=now()+interval '5 minutes' WHERE token=$1`, g.Token); err != nil {
		t.Fatal(err)
	}
	renewed, err := s.StableGrantFor(ctx, org, "doc-20", "alice", "res", "obj", "application/pdf", "a.pdf")
	if err != nil {
		t.Fatal(err)
	}
	if renewed.Token == g.Token {
		t.Fatalf("a grant inside the renewal window must not be reused")
	}
	if renewed.ExpiresAt == nil || time.Until(*renewed.ExpiresAt) < fileGrantRenewMargin {
		t.Fatalf("renewed grant must outlive the renewal window: %v", renewed.ExpiresAt)
	}
	if _, err := s.FileGrantByToken(ctx, g.Token); err == nil {
		t.Fatalf("the superseded near-expiry grant must be revoked")
	}
}

// ObservePhase accepts the seven contract phases and folds unknown
// names into "other" without panicking; FederationFields omits empty keys.
func TestObservePhaseAndFederationFields(t *testing.T) {
	for _, p := range []string{"discovery", "input", "queue", "retrieval", "generation", "verify", "delivery", "bogus-phase"} {
		obs.ObservePhase(p, time.Millisecond)
	}
	root := "r"
	f := obs.FederationFields{RootTaskID: &root}
	fields := f.Fields()
	if len(fields) != 2 || fields[0] != "root_task_id" || fields[1] != "r" {
		t.Fatalf("federation fields wrong: %v", fields)
	}
	if len((obs.FederationFields{}).Fields()) != 0 {
		t.Fatalf("empty correlation must emit no log keys")
	}
}
