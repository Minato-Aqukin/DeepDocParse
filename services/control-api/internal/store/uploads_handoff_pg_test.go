package store

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/auth"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/migrate"
)

// 上传资源交接的 store 回归：只验不可变事件与状态迁移，不做字段拷贝断言。
//
// 约定：CONTROL_TEST_DATABASE_URL 指向真 PG（见 usage_pg_test.go）；没有就跳过，
// CI 的 go job 起 postgres，那里真跑。

func handoffStore(t *testing.T) (*Store, string) {
	t.Helper()
	s := &Store{pool: testPool(t)}
	if _, err := migrate.Up(context.Background(), s.pool); err != nil {
		t.Fatal(err)
	}
	return s, seedOrg(t, s)
}

func handoffClaim(t *testing.T, s *Store, org, actor, key, digest string, target *string) *UploadSession {
	t.Helper()
	sha := strings.Repeat("c", 64)
	part := int64(5 << 20)
	u, created, err := s.ClaimUpload(context.Background(), &UploadSession{
		OrganizationID: org, ActorID: actor, ActorKind: "user",
		ObjectKey: "uploads/" + org + "/" + auth.NewID(),
		Filename:  "input.pdf", MIME: "application/pdf",
		DeclaredSize: 20, DeclaredSHA256: &sha,
		ExpiresAt:            time.Now().Add(time.Hour),
		CreateIdempotencyKey: &key, RequestDigest: &digest, PartSize: &part,
		TargetResourceID: target,
	}, 1)
	if err != nil || !created {
		t.Fatalf("claim target upload: %+v %v", u, err)
	}
	return u
}

func handoffReady(t *testing.T, s *Store, org string, u *UploadSession) {
	t.Helper()
	ctx := context.Background()
	if _, _, err := s.FinalizeUpload(ctx, org, u.ID, "fin-"+u.ID, 20, "", json.RawMessage(`{}`)); err != nil {
		t.Fatal(err)
	}
	// verify 只认领取租约：先 PendingVerification 领一行（单测里就是这一行），
	// 再 MarkUploadVerified —— 与 verifyUploads 生产路径同顺序。
	claimed, err := s.PendingVerification(ctx, 16)
	if err != nil {
		t.Fatal(err)
	}
	found := false
	for _, c := range claimed {
		if c.ID == u.ID {
			found = true
		}
	}
	if !found {
		t.Fatalf("upload not claimed by PendingVerification: %s", u.ID)
	}
	if err := s.MarkUploadVerified(ctx, org, u.ID, strings.Repeat("c", 64)); err != nil {
		t.Fatal(err)
	}
}

func handoffEventPayload(t *testing.T, s *Store, org, uploadID string) (string, map[string]any) {
	t.Helper()
	var id string
	var payload json.RawMessage
	err := s.pool.QueryRow(context.Background(),
		`SELECT id, payload FROM control.control_outbox WHERE organization_id=$1 AND type='DocumentSubmitted' AND payload->>'upload_id'=$2`,
		org, uploadID).Scan(&id, &payload)
	if err != nil {
		t.Fatalf("DocumentSubmitted missing: %v", err)
	}
	var m map[string]any
	if err := json.Unmarshal(payload, &m); err != nil {
		t.Fatal(err)
	}
	return id, m
}

// 目标随会话冻结：verify 后读回的会话带目标，事件载荷里的 target 与创建值一致，
// 且是存储行的值 —— finalize 之后再改行也影响不了已发出的事件。
func TestUploadHandoffTargetPersistsThroughVerifyEvent(t *testing.T) {
	s, org := handoffStore(t)
	ctx := context.Background()
	target := "res-live-1"
	u := handoffClaim(t, s, org, "alice", auth.NewID(), "sha256:"+strings.Repeat("d", 64), &target)
	if got, err := s.UploadSession(ctx, org, u.ID); err != nil || got.TargetResourceID == nil || *got.TargetResourceID != target {
		t.Fatalf("target not frozen at create: %+v %v", got, err)
	}
	handoffReady(t, s, org, u)
	got, err := s.UploadSession(ctx, org, u.ID)
	if err != nil || got.TargetResourceID == nil || *got.TargetResourceID != target {
		t.Fatalf("target lost after verify: %+v %v", got, err)
	}
	_, m := handoffEventPayload(t, s, org, u.ID)
	if m["target_resource_id"] != target {
		t.Fatalf("event target not bound from stored row: %v", m)
	}
}

// 同一创建键换目标重放是幂等冲突，且第一次的目标不被覆盖。
func TestUploadHandoffDigestReplayConflictPreservesFirstTarget(t *testing.T) {
	s, org := handoffStore(t)
	key := auth.NewID()
	first := "res-first"
	u := handoffClaim(t, s, org, "alice", key, "sha256:"+strings.Repeat("e", 64), &first)
	other := "res-other"
	sha := strings.Repeat("c", 64)
	part := int64(5 << 20)
	_, _, err := s.ClaimUpload(context.Background(), &UploadSession{
		OrganizationID: org, ActorID: "alice", ActorKind: "user",
		ObjectKey: "uploads/" + org + "/" + auth.NewID(),
		Filename:  "input.pdf", MIME: "application/pdf",
		DeclaredSize: 20, DeclaredSHA256: &sha,
		ExpiresAt:            time.Now().Add(time.Hour),
		CreateIdempotencyKey: &key, RequestDigest: new("sha256:" + strings.Repeat("f", 64)), PartSize: &part,
		TargetResourceID: &other,
	}, 1)
	if !errors.Is(err, ErrUploadIdempotencyConflict) {
		t.Fatalf("changed target replay: %v", err)
	}
	got, err := s.UploadSession(context.Background(), org, u.ID)
	if err != nil || got.TargetResourceID == nil || *got.TargetResourceID != first {
		t.Fatalf("first target overwritten: %+v %v", got, err)
	}
}

// 同键同摘要重放返回原会话：目标一致才不是冲突。
func TestUploadHandoffIdenticalReplayReusesSession(t *testing.T) {
	s, org := handoffStore(t)
	key := auth.NewID()
	digest := "sha256:" + strings.Repeat("a", 64)
	target := "res-same"
	u := handoffClaim(t, s, org, "alice", key, digest, &target)
	sha := strings.Repeat("c", 64)
	part := int64(5 << 20)
	again, created, err := s.ClaimUpload(context.Background(), &UploadSession{
		OrganizationID: org, ActorID: "alice", ActorKind: "user",
		ObjectKey: "uploads/" + org + "/" + auth.NewID(),
		Filename:  "input.pdf", MIME: "application/pdf",
		DeclaredSize: 20, DeclaredSHA256: &sha,
		ExpiresAt:            time.Now().Add(time.Hour),
		CreateIdempotencyKey: &key, RequestDigest: &digest, PartSize: &part,
		TargetResourceID: &target,
	}, 1)
	if err != nil || created || again.ID != u.ID {
		t.Fatalf("identical replay diverged: %+v %v %v", again, created, err)
	}
}

// 字节未 ready 前 ingest 恒 null；ready 后缺事件是 pending，不是 ready。
func TestUploadHandoffIngestNullUntilByteReadyThenPending(t *testing.T) {
	s, org := handoffStore(t)
	ctx := context.Background()
	target := "res-null-then-pending"
	u := handoffClaim(t, s, org, "alice", auth.NewID(), "sha256:"+strings.Repeat("b", 64), &target)
	if got, err := s.UploadSession(ctx, org, u.ID); err != nil || got.IngestStatus != nil || got.IngestError != nil {
		t.Fatalf("pre-ready ingest must be null: %+v %v", got, err)
	}
	if _, _, err := s.FinalizeUpload(ctx, org, u.ID, "fin-"+u.ID, 20, "", json.RawMessage(`{}`)); err != nil {
		t.Fatal(err)
	}
	if got, err := s.UploadSession(ctx, org, u.ID); err != nil || got.IngestStatus != nil {
		t.Fatalf("verifying ingest must be null: %+v %v", got, err)
	}
	// 事件缺失的 ready：删掉 verify 将要发出的事件不可行（它与状态同一事务），
	// 用孤立 ready 行模拟"状态已 ready、事件尚未落库"的中间态是不可能的——
	// 这里直接删事件，读出必须是 pending 而不是 ready。
	handoffReady(t, s, org, u)
	if _, err := s.pool.Exec(ctx, `DELETE FROM control.control_outbox WHERE organization_id=$1 AND payload->>'upload_id'=$2`, org, u.ID); err != nil {
		t.Fatal(err)
	}
	got, err := s.UploadSession(ctx, org, u.ID)
	if err != nil || got.IngestStatus == nil || *got.IngestStatus != "pending" {
		t.Fatalf("missing event must read pending: %+v %v", got, err)
	}
}

// ready + 未投递事件是 pending；一次瞬时失败后是 retrying，且错误是固定文案。
func TestUploadHandoffIngestPendingThenRetrying(t *testing.T) {
	s, org := handoffStore(t)
	ctx := context.Background()
	u := handoffClaim(t, s, org, "alice", auth.NewID(), "sha256:"+strings.Repeat("1", 64), new("res-pending"))
	handoffReady(t, s, org, u)
	eventID, _ := handoffEventPayload(t, s, org, u.ID)
	got, err := s.UploadSession(ctx, org, u.ID)
	if err != nil || got.IngestStatus == nil || *got.IngestStatus != "pending" || got.IngestError != nil {
		t.Fatalf("undelivered event must read pending: %+v %v", got, err)
	}
	claimed, err := s.ClaimOutbox(ctx, 32)
	if err != nil || len(claimed) == 0 {
		t.Fatalf("event not claimable: %v %d", err, len(claimed))
	}
	if err := s.MarkOutboxFailed(ctx, eventID, claimed[0].Attempts, "corpus-api 返回 502"); err != nil {
		t.Fatal(err)
	}
	// 退避未到前 next_attempt_at 在未来：把时钟拨回调 fake 会污染其它测试，
	// 这里直接读 ingest 派生位即可（派生不看 next_attempt_at）。
	got, err = s.UploadSession(ctx, org, u.ID)
	if err != nil || got.IngestStatus == nil || *got.IngestStatus != "retrying" {
		t.Fatalf("failed delivery must read retrying: %+v %v", got, err)
	}
	// 上游原因（可能带内部地址/密钥）只留在 outbox 行上，不进上传者可读的字段
	if got.IngestError != nil {
		t.Fatalf("retry cause leaked to uploader: %q", *got.IngestError)
	}
	var lastErr *string
	if err := s.pool.QueryRow(ctx, `SELECT last_error FROM control.control_outbox WHERE id=$1`, eventID).Scan(&lastErr); err != nil {
		t.Fatal(err)
	}
	if lastErr == nil || !strings.Contains(*lastErr, "502") {
		t.Fatalf("diagnostic cause lost from outbox row: %v", lastErr)
	}
}

// 确定性拒绝：事件离开投递队列，读出是 rejected + 安全码；重领不再拿到它。
//
// 领取是全局的（测试库里有别的包、别的组织的事件），所以先证明这条事件
// **拒绝前确实领得到**，拒绝后领不到才有意义。积压计数同样是全局口径，
// 在共享库里隔离不出来，这里不断言它 —— 断言它只会随并行测试时红时绿。
func TestUploadHandoffRejectedLeavesQueueAndReadsRejected(t *testing.T) {
	s, org := handoffStore(t)
	ctx := context.Background()
	u := handoffClaim(t, s, org, "alice", auth.NewID(), "sha256:"+strings.Repeat("2", 64), new("res-rejected"))
	handoffReady(t, s, org, u)
	eventID, _ := handoffEventPayload(t, s, org, u.ID)
	claimedIDs := func() map[string]bool {
		claimed, err := s.ClaimOutbox(ctx, 1000)
		if err != nil {
			t.Fatal(err)
		}
		ids := map[string]bool{}
		for _, e := range claimed {
			ids[e.ID] = true
		}
		return ids
	}
	if !claimedIDs()[eventID] {
		t.Fatal("precondition: undelivered event was not claimable before rejection")
	}
	if err := s.MarkOutboxRejected(ctx, eventID, "invalid_upload_target"); err != nil {
		t.Fatal(err)
	}
	if claimedIDs()[eventID] {
		t.Fatal("rejected event re-claimed")
	}
	got, err := s.UploadSession(ctx, org, u.ID)
	if err != nil || got.IngestStatus == nil || *got.IngestStatus != "rejected" {
		t.Fatalf("rejected delivery must read rejected: %+v %v", got, err)
	}
	if got.IngestError == nil || *got.IngestError != "invalid_upload_target" {
		t.Fatalf("rejected error must be safe code: %+v", got.IngestError)
	}
}

// 真实 ACK 是权威的：清拒绝痕迹，读出 ready；迟到的拒绝盖不掉已投递。
func TestUploadHandoffAckClearsRejectionLateRejectNoOverwrite(t *testing.T) {
	s, org := handoffStore(t)
	ctx := context.Background()
	u := handoffClaim(t, s, org, "alice", auth.NewID(), "sha256:"+strings.Repeat("3", 64), new("res-ack"))
	handoffReady(t, s, org, u)
	eventID, _ := handoffEventPayload(t, s, org, u.ID)
	if err := s.MarkOutboxRejected(ctx, eventID, "invalid_upload_target"); err != nil {
		t.Fatal(err)
	}
	if err := s.MarkOutboxDelivered(ctx, eventID); err != nil {
		t.Fatal(err)
	}
	if err := s.MarkOutboxRejected(ctx, eventID, "invalid_upload_target"); err != nil {
		t.Fatal(err)
	}
	got, err := s.UploadSession(ctx, org, u.ID)
	if err != nil || got.IngestStatus == nil || *got.IngestStatus != "ready" || got.IngestError != nil {
		t.Fatalf("ACK must win over rejection: %+v %v", got, err)
	}
	var rejectedAt *time.Time
	if err := s.pool.QueryRow(ctx, `SELECT rejected_at FROM control.control_outbox WHERE id=$1`, eventID).Scan(&rejectedAt); err != nil {
		t.Fatal(err)
	}
	if rejectedAt != nil {
		t.Fatal("late rejection overwrote delivery")
	}
}

// 409 duplicate_event 也是 ACK：调用方按 MarkOutboxDelivered 确认后读 ready。
func TestUploadHandoffDuplicateEventAckReadsReady(t *testing.T) {
	s, org := handoffStore(t)
	ctx := context.Background()
	u := handoffClaim(t, s, org, "alice", auth.NewID(), "sha256:"+strings.Repeat("4", 64), nil)
	handoffReady(t, s, org, u)
	eventID, m := handoffEventPayload(t, s, org, u.ID)
	if _, ok := m["target_resource_id"]; ok {
		t.Fatalf("untargeted event must omit target key: %v", m)
	}
	// 409 由投递器判 ACK（background.go），store 侧的确认动作就是 Delivered。
	if err := s.MarkOutboxDelivered(ctx, eventID); err != nil {
		t.Fatal(err)
	}
	got, err := s.UploadSession(ctx, org, u.ID)
	if err != nil || got.IngestStatus == nil || *got.IngestStatus != "ready" {
		t.Fatalf("duplicate-event ACK must read ready: %+v %v", got, err)
	}
}
