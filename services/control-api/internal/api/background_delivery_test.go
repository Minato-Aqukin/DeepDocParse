package api

import (
	"testing"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/contracts"
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
