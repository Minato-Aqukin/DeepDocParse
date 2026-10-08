package api

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/auth"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/contracts"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/store"
)

// 投递回应的三分类是"已登记"这句话的唯一依据：把确定性 409 当 ACK，上传者会看到
// "已登记"而语料库里什么都没有；把可恢复的失败当终态，已校验的上传永远进不了库。
func TestDeliveryClassificationSeparatesAckRejectionAndRetry(t *testing.T) {
	envelope := func(code string) []byte {
		return []byte(`{"error":{"message":"x","type":"invalid_request_error","code":"` + code + `"}}`)
	}
	cases := []struct {
		name   string
		typ    string
		status int
		body   []byte
		want   deliveryOutcome
		code   contracts.IngestRejection
	}{
		{"first delivery", "DocumentSubmitted", 200, []byte(`{"ok":true}`), deliveryAcked, ""},
		{"replayed event", "DocumentSubmitted", 409, envelope("duplicate_event"), deliveryAcked, ""},
		{"duplicate bytes in target", "DocumentSubmitted", 409, envelope("resource_version_exists"), deliveryRejected, contracts.IngestRejectionResourceVersionExists},
		{"target deleted", "DocumentSubmitted", 404, envelope("resource_not_found"), deliveryRejected, contracts.IngestRejectionResourceNotFound},
		{"revival race says retry", "DocumentSubmitted", 409, envelope("document_state_changed"), deliveryRetry, ""},
		{"route missing, no envelope", "DocumentSubmitted", 404, []byte(`{"detail":"Not Found"}`), deliveryRetry, ""},
		{"corpus down", "DocumentSubmitted", 502, envelope("service_unavailable"), deliveryRetry, ""},
		{"credentials misconfigured", "DocumentSubmitted", 403, envelope("resource_not_found"), deliveryRetry, ""},
		{"rejection codes are DocumentSubmitted-only", "DocumentDeleted", 404, envelope("resource_not_found"), deliveryRetry, ""},
	}
	for _, c := range cases {
		got, code, _ := classifyDelivery(c.typ, c.status, c.body)
		if got != c.want || code != c.code {
			t.Errorf("%s: outcome=%v code=%q, want %v %q", c.name, got, code, c.want, c.code)
		}
	}
}

// 每次实际投递（成功/拒绝/失败/网络错）都记一次 delivery 阶段耗时。
// 删掉 deliverEvent 里的 ObservePhase 调用，这个测试就红：它数的是
// delivery 直方图的样本数，不是 helper 的存在。
func TestDeliverEventObservesDeliveryPhaseOnEveryOutcome(t *testing.T) {
	f := discoveryPGFixture(t)
	before := deliverySampleCount(t)
	ctx := context.Background()

	mustEvent := func() store.OutboxEvent {
		t.Helper()
		id := auth.NewID()
		if _, err := f.server.store.Pool().Exec(ctx,
			`INSERT INTO control.control_outbox (id, organization_id, type, payload) VALUES ($1,$2,'DocumentSubmitted','{}')`,
			id, f.org); err != nil {
			t.Fatal(err)
		}
		return store.OutboxEvent{ID: id, OrganizationID: f.org, Type: "DocumentSubmitted", Payload: json.RawMessage(`{}`)}
	}

	// 成功与拒绝：两个假 corpus 各回一种状态。
	acker := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"ok":true}`))
	}))
	defer acker.Close()
	rejector := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusConflict)
		_, _ = w.Write([]byte(`{"error":{"code":"resource_version_exists"}}`))
	}))
	defer rejector.Close()
	client := &http.Client{Timeout: 5 * time.Second}

	f.server.cfg.CorpusURL = acker.URL
	f.server.deliverEvent(ctx, client, mustEvent())
	f.server.cfg.CorpusURL = rejector.URL
	f.server.deliverEvent(ctx, client, mustEvent())

	// 网络错：连一个必然拒绝的端口，同样记一次。
	f.server.cfg.CorpusURL = "http://127.0.0.1:1"
	f.server.deliverEvent(ctx, client, mustEvent())

	if got := deliverySampleCount(t); got-before != 3 {
		t.Fatalf("delivery observations=%d, want 3 (ack + reject + network error)", got-before)
	}
}

func deliverySampleCount(t *testing.T) int {
	t.Helper()
	mfs, err := prometheus.DefaultGatherer.Gather()
	if err != nil {
		t.Fatal(err)
	}
	for _, mf := range mfs {
		if mf.GetName() != "ddp_control_phase_duration_seconds" {
			continue
		}
		for _, m := range mf.GetMetric() {
			for _, lp := range m.GetLabel() {
				if lp.GetName() == "phase" && lp.GetValue() == "delivery" {
					return int(m.GetHistogram().GetSampleCount())
				}
			}
		}
	}
	return 0
}
