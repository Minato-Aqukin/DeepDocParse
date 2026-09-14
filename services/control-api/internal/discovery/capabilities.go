package discovery

import (
	"bytes"
	"encoding/json"
	"regexp"
	"time"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/contracts"
)

type CapabilityInputContract struct {
	Formats  *[]string `json:"formats,omitempty"`
	MaxBytes *int64    `json:"max_bytes,omitempty"`
}

type CapabilityLimits struct {
	MaxConcurrency *int64 `json:"max_concurrency,omitempty"`
	MaxCandidates  *int64 `json:"max_candidates,omitempty"`
}

// CapabilityProfile separates producer-observed health from admission permission.
// AcceptingAdmissions is the producer's current claim (switch + store + operation
// readiness); control passes it through and never invents it locally.
type CapabilityProfile struct {
	Schema              string                        `json:"schema"`
	NodeID              string                        `json:"node_id"`
	Operation           string                        `json:"operation"`
	Profile             *string                       `json:"profile,omitempty"`
	Configured          *bool                         `json:"configured,omitempty"`
	Readiness           contracts.CapabilityReadiness `json:"readiness"`
	AcceptingAdmissions bool                          `json:"accepting_admissions"`
	EngineVersions      map[string]string             `json:"engine_versions,omitempty"`
	InputContract       *CapabilityInputContract      `json:"input_contract,omitempty"`
	Limits              *CapabilityLimits             `json:"limits,omitempty"`
	ObservedAt          time.Time                     `json:"observed_at"`
	ValidUntil          time.Time                     `json:"valid_until"`
}

var capabilityNodeID = regexp.MustCompile(`^[a-z0-9][a-z0-9._-]{2,63}$`)

var capabilityOperations = map[string]bool{
	"corpus.retrieve": true, "corpus.locate": true,
	"doc.parse": true, "doc.compile": true, "rag.answer.cited": true,
	"extract.fields": true, "wiki.pages": true, "rerank": true,
}

// ProjectProfiles validates a complete observation and projects only contract
// fields. A malformed or stale profile invalidates the whole observation; no
// partial ready result can hide an unavailable producer. Unknown fields are
// ignored rather than reflected into public capability or handshake responses.
func ProjectProfiles(body []byte, nodeID string, now time.Time) ([]CapabilityProfile, string) {
	unknown := func() ([]CapabilityProfile, string) { return []CapabilityProfile{}, "unknown" }
	if len(body) > 1<<20 || !capabilityNodeID.MatchString(nodeID) {
		return unknown()
	}
	var envelope map[string]json.RawMessage
	if json.Unmarshal(body, &envelope) != nil || envelope == nil {
		return unknown()
	}
	status, ok := capabilityField[string](envelope, "capability_status", true)
	if !ok || *status != "observed" {
		return unknown()
	}
	profiles, ok := capabilityField[[]map[string]json.RawMessage](envelope, "profiles", true)
	if !ok || len(*profiles) > 128 {
		return unknown()
	}
	projected := make([]CapabilityProfile, 0, len(*profiles))
	for _, raw := range *profiles {
		p, ok := projectProfile(raw, nodeID, now)
		if !ok {
			return unknown()
		}
		projected = append(projected, p)
	}
	return projected, "observed"
}

// A pointer records actual presence. JSON null is not a substitute for a
// required false, zero, empty string, or object, including optional fields.
func capabilityField[T any](raw map[string]json.RawMessage, key string, required bool) (*T, bool) {
	b, present := raw[key]
	if !present {
		return nil, !required
	}
	if bytes.Equal(bytes.TrimSpace(b), []byte("null")) {
		return nil, false
	}
	var value T
	if json.Unmarshal(b, &value) != nil {
		return nil, false
	}
	return &value, true
}

func projectProfile(raw map[string]json.RawMessage, nodeID string, now time.Time) (CapabilityProfile, bool) {
	bad := func() (CapabilityProfile, bool) { return CapabilityProfile{}, false }
	schema, ok := capabilityField[string](raw, "schema", true)
	if !ok || *schema != "ddp-discovery/1#CapabilityProfile" {
		return bad()
	}
	operation, ok := capabilityField[string](raw, "operation", true)
	if !ok || !capabilityOperations[*operation] {
		return bad()
	}
	readiness, ok := capabilityField[contracts.CapabilityReadiness](raw, "readiness", true)
	if !ok || !readiness.Valid() {
		return bad()
	}
	admissions, ok := capabilityField[bool](raw, "accepting_admissions", true)
	if !ok {
		return bad()
	}
	observed, ok := capabilityField[time.Time](raw, "observed_at", true)
	if !ok || observed.After(now.Add(time.Minute)) {
		return bad()
	}
	valid, ok := capabilityField[time.Time](raw, "valid_until", true)
	if !ok || !valid.After(now) || !valid.After(*observed) {
		return bad()
	}
	// Admission permission is the producer's claim about *its own* current
	// willingness to accept work; it must pass through, not be hardcoded false.
	// Missing/null/non-bool invalidates the whole observation (checked above),
	// so an absent value can never be laundered into "accepts work".
	p := CapabilityProfile{Schema: *schema, NodeID: nodeID, Operation: *operation, Readiness: *readiness, ObservedAt: *observed, ValidUntil: *valid, AcceptingAdmissions: *admissions}
	if p.Profile, ok = capabilityField[string](raw, "profile", false); !ok {
		return bad()
	}
	if p.Configured, ok = capabilityField[bool](raw, "configured", false); !ok {
		return bad()
	}
	versions, ok := capabilityField[map[string]*string](raw, "engine_versions", false)
	if !ok {
		return bad()
	}
	if versions != nil {
		p.EngineVersions = make(map[string]string, len(*versions))
		for name, version := range *versions {
			if version == nil {
				return bad()
			}
			p.EngineVersions[name] = *version
		}
	}
	input, ok := capabilityField[map[string]json.RawMessage](raw, "input_contract", false)
	if !ok {
		return bad()
	}
	if input != nil {
		p.InputContract = &CapabilityInputContract{}
		formats, ok := capabilityField[[]*string](*input, "formats", false)
		if !ok {
			return bad()
		}
		if formats != nil {
			values := make([]string, 0, len(*formats))
			for _, format := range *formats {
				if format == nil {
					return bad()
				}
				values = append(values, *format)
			}
			p.InputContract.Formats = &values
		}
		p.InputContract.MaxBytes, ok = capabilityField[int64](*input, "max_bytes", false)
		if !ok || (p.InputContract.MaxBytes != nil && *p.InputContract.MaxBytes < 1) {
			return bad()
		}
	}
	limits, ok := capabilityField[map[string]json.RawMessage](raw, "limits", false)
	if !ok {
		return bad()
	}
	if limits != nil {
		p.Limits = &CapabilityLimits{}
		p.Limits.MaxConcurrency, ok = capabilityField[int64](*limits, "max_concurrency", false)
		if !ok || (p.Limits.MaxConcurrency != nil && *p.Limits.MaxConcurrency < 0) {
			return bad()
		}
		p.Limits.MaxCandidates, ok = capabilityField[int64](*limits, "max_candidates", false)
		if !ok || (p.Limits.MaxCandidates != nil && *p.Limits.MaxCandidates < 1) {
			return bad()
		}
	}
	return p, true
}
