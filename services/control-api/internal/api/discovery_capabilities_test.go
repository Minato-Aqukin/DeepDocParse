package api

import (
	"testing"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/contracts"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/discovery"
)

// The node-level admission roll-up must follow the producer, not a local
// constant: unknown observations and empty profile sets stay false, and a
// single observed accepting profile is enough to advertise the node as open.
func TestAcceptingAdmissionsRollup(t *testing.T) {
	accepts := discovery.CapabilityProfile{Readiness: contracts.CapabilityReadinessReady, AcceptingAdmissions: true}
	declines := discovery.CapabilityProfile{Readiness: contracts.CapabilityReadinessReady, AcceptingAdmissions: false}

	cases := []struct {
		name     string
		status   string
		profiles []discovery.CapabilityProfile
		want     bool
	}{
		{"unknown status stays closed", "unknown", []discovery.CapabilityProfile{accepts}, false},
		{"empty observation stays closed", "observed", nil, false},
		{"all declining stays closed", "observed", []discovery.CapabilityProfile{declines}, false},
		{"one accepting opens the node", "observed", []discovery.CapabilityProfile{declines, accepts}, true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := acceptingAdmissions(tc.profiles, tc.status); got != tc.want {
				t.Fatalf("acceptingAdmissions(%v, %q) = %v, want %v", tc.profiles, tc.status, got, tc.want)
			}
		})
	}
}
