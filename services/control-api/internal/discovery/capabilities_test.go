package discovery

import (
	"encoding/json"
	"strings"
	"testing"
	"time"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/contracts"
)

var capabilityTestNow = time.Date(2026, 9, 12, 8, 0, 0, 0, time.UTC)

func profileFixture() map[string]any {
	return map[string]any{
		"schema": "ddp-discovery/1#CapabilityProfile", "operation": "rag.answer.cited",
		"readiness": "ready", "accepting_admissions": true,
		"observed_at": capabilityTestNow.Add(-time.Minute).Format(time.RFC3339),
		"valid_until": capabilityTestNow.Add(time.Minute).Format(time.RFC3339),
	}
}

func encodedProfiles(t *testing.T, profiles ...map[string]any) []byte {
	t.Helper()
	body, err := json.Marshal(map[string]any{"capability_status": "observed", "profiles": profiles})
	if err != nil {
		t.Fatal(err)
	}
	return body
}

func TestProjectProfilesAllowsOnlyTypedContractFields(t *testing.T) {
	p := profileFixture()
	p["node_id"] = map[string]string{"forged": "upstream-authority"}
	p["secret"] = "must-not-escape"
	p["internal_endpoint"] = "must-not-escape"
	p["configured"] = false
	p["profile"] = "cited-v1"
	p["engine_versions"] = map[string]string{"instruction_model": "model-v2"}
	p["input_contract"] = map[string]any{"formats": []string{"pdf"}, "max_bytes": 1024, "service_token": "must-not-escape"}
	p["limits"] = map[string]any{"max_concurrency": 0, "max_candidates": 5, "internal_endpoint": "must-not-escape"}
	got, status := ProjectProfiles(encodedProfiles(t, p), "node-local", capabilityTestNow)
	if status != "observed" || len(got) != 1 {
		t.Fatalf("expected observed profile, got %s", status)
	}
	if got[0].NodeID != "node-local" || got[0].Readiness != contracts.CapabilityReadinessReady || !got[0].AcceptingAdmissions {
		t.Fatal("producer authority was rewritten or admission permission was dropped through projection")
	}
	if got[0].Configured == nil || *got[0].Configured || got[0].Limits.MaxConcurrency == nil || *got[0].Limits.MaxConcurrency != 0 {
		t.Fatal("explicit false or zero did not survive projection")
	}
	encoded, err := json.Marshal(got)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(encoded), "must-not-escape") || strings.Contains(string(encoded), "upstream-authority") {
		t.Fatal("unknown or internal fields were echoed")
	}
	if !strings.Contains(string(encoded), `"formats":["pdf"]`) || !strings.Contains(string(encoded), `"max_bytes":1024`) || !strings.Contains(string(encoded), `"instruction_model":"model-v2"`) {
		t.Fatal("valid optional contract fields were lost")
	}
}

func TestProjectProfilesPassesThroughTheProducerAdmissionClaim(t *testing.T) {
	for _, want := range []bool{true, false} {
		p := profileFixture()
		p["accepting_admissions"] = want
		got, status := ProjectProfiles(encodedProfiles(t, p), "node-local", capabilityTestNow)
		if status != "observed" || len(got) != 1 || got[0].AcceptingAdmissions != want {
			t.Fatalf("producer admission claim %v was not passed through", want)
		}
	}
}

func TestProjectProfilesPreservesObservedUnknownWithoutInferringReadiness(t *testing.T) {
	for _, readiness := range contracts.CapabilityReadinessValues {
		t.Run(string(readiness), func(t *testing.T) {
			p := profileFixture()
			p["readiness"] = readiness
			p["configured"] = true
			got, status := ProjectProfiles(encodedProfiles(t, p), "node-local", capabilityTestNow)
			if status != "observed" || len(got) != 1 || got[0].Readiness != readiness || !got[0].AcceptingAdmissions {
				t.Fatal("readiness was inferred or the producer's admission claim was dropped")
			}
		})
	}
}

func TestProjectProfilesRequiresEveryRequiredField(t *testing.T) {
	for _, key := range []string{"schema", "operation", "readiness", "accepting_admissions", "observed_at", "valid_until"} {
		for _, missing := range []bool{true, false} {
			name := key + "/null"
			if missing {
				name = key + "/missing"
			}
			t.Run(name, func(t *testing.T) {
				p := profileFixture()
				if missing {
					delete(p, key)
				} else {
					p[key] = nil
				}
				assertProfilesUnknown(t, encodedProfiles(t, p))
			})
		}
	}
}

func TestProjectProfilesRejectsMalformedAndStaleProfiles(t *testing.T) {
	cases := []struct {
		name, key string
		value     any
	}{
		{"schema", "schema", "wrong/1"},
		{"operation", "operation", "shell.exec"},
		{"operation_type", "operation", []string{"doc.parse"}},
		{"readiness", "readiness", "online"},
		{"admissions_type", "accepting_admissions", "false"},
		{"expired", "valid_until", capabilityTestNow.Add(-time.Second)},
		{"expires_now", "valid_until", capabilityTestNow},
		{"bad_date", "observed_at", "yesterday"},
		{"future_observation", "observed_at", capabilityTestNow.Add(61 * time.Second)},
		{"expiry_not_after_observation", "observed_at", capabilityTestNow.Add(time.Minute)},
		{"profile_type", "profile", map[string]string{"secret": "hidden"}},
		{"configured_type", "configured", "true"},
		{"configured_null", "configured", nil},
		{"versions_type", "engine_versions", []string{"v1"}},
		{"version_value_type", "engine_versions", map[string]any{"model": 2}},
		{"version_value_null", "engine_versions", map[string]any{"model": nil}},
		{"input_type", "input_contract", "pdf"},
		{"format_type", "input_contract", map[string]any{"formats": "pdf"}},
		{"format_entry_type", "input_contract", map[string]any{"formats": []any{"pdf", true}}},
		{"format_entry_null", "input_contract", map[string]any{"formats": []any{nil}}},
		{"format_null", "input_contract", map[string]any{"formats": nil}},
		{"max_bytes_zero", "input_contract", map[string]any{"max_bytes": 0}},
		{"max_bytes_fraction", "input_contract", map[string]any{"max_bytes": 1.5}},
		{"max_bytes_null", "input_contract", map[string]any{"max_bytes": nil}},
		{"limits_type", "limits", true},
		{"concurrency_negative", "limits", map[string]any{"max_concurrency": -1}},
		{"concurrency_type", "limits", map[string]any{"max_concurrency": "0"}},
		{"candidates_zero", "limits", map[string]any{"max_candidates": 0}},
		{"candidates_null", "limits", map[string]any{"max_candidates": nil}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			p := profileFixture()
			p[tc.key] = tc.value
			// A valid first item must not cause partial ready results on failure.
			assertProfilesUnknown(t, encodedProfiles(t, profileFixture(), p))
		})
	}
}

func TestProjectProfilesRequiresExplicitObservedEnvelope(t *testing.T) {
	valid, _ := json.Marshal(profileFixture())
	for _, body := range []string{
		`{`, `null`, `[]`, `{} `,
		`{"profiles":[` + string(valid) + `]}`,
		`{"capability_status":"unknown","profiles":[` + string(valid) + `]}`,
		`{"capability_status":true,"profiles":[]}`,
		`{"capability_status":"observed"}`,
		`{"capability_status":"observed","profiles":null}`,
		`{"capability_status":"observed","profiles":{}}`,
		`{"capability_status":"observed","profiles":[null]}`,
		`{"capability_status":"observed","profiles":[]} {}`,
	} {
		assertProfilesUnknown(t, []byte(body))
	}
	got, status := ProjectProfiles([]byte(`{"capability_status":"observed","profiles":[]}`), "node-local", capabilityTestNow)
	if status != "observed" || got == nil || len(got) != 0 {
		t.Fatal("explicit empty observation must remain an empty list")
	}
	oversized := make([]map[string]any, 129)
	for n := range oversized {
		oversized[n] = profileFixture()
	}
	assertProfilesUnknown(t, encodedProfiles(t, oversized...))
}

func assertProfilesUnknown(t *testing.T, body []byte) {
	t.Helper()
	profiles, status := ProjectProfiles(body, "node-local", capabilityTestNow)
	if status != "unknown" || profiles == nil || len(profiles) != 0 {
		t.Fatalf("untrusted observation produced %s with %d profiles", status, len(profiles))
	}
}
