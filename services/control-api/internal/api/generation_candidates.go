package api

import (
	"context"
	"net/http"
	"sync"
	"time"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/apierr"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/contracts"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/discovery"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/httpx"
)

type generationCandidate struct {
	NodeID               string                        `json:"node_id"`
	State                string                        `json:"state"`
	DescriptorValidUntil time.Time                     `json:"descriptor_valid_until"`
	Operation            string                        `json:"operation"`
	Readiness            contracts.CapabilityReadiness `json:"readiness"`
	AcceptingAdmissions  bool                          `json:"accepting_admissions"`
	ObservedAt           *time.Time                    `json:"observed_at,omitempty"`
	ValidUntil           *time.Time                    `json:"valid_until,omitempty"`
}

func (s *Server) handleGenerationCandidates(w http.ResponseWriter, r *http.Request) error {
	if err := s.discoveryReady(); err != nil {
		return err
	}
	a, err := mustActor(r)
	if err != nil {
		return err
	}
	query := r.URL.Query()
	operations := query["operation"]
	if len(query) != 1 || len(operations) != 1 || (operations[0] != "rag.answer.cited" && operations[0] != "wiki.pages") {
		return apierr.BadRequest("invalid_generation_operation", "operation must be rag.answer.cited or wiki.pages")
	}
	members, err := s.store.GenerationMembers(r.Context(), a.OrganizationID, a.UserID, s.nodeIdentity.NodeID(), a.Role.CanManageOrg())
	if err != nil {
		return err
	}
	items := make([]generationCandidate, len(members))
	// Bound both the entire read and each peer. Independent observations use a
	// small worker pool so a blackholed member cannot starve later healthy peers.
	ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
	defer cancel()
	jobs := make(chan int)
	var workers sync.WaitGroup
	for range min(4, len(members)) {
		workers.Add(1)
		go func() {
			defer workers.Done()
			for index := range jobs {
				if item, ok := s.observeGenerationCandidate(ctx, &members[index], operations[0]); ok {
					items[index] = item
				}
			}
		}()
	}
	for index := range members {
		jobs <- index
	}
	close(jobs)
	workers.Wait()
	// Re-check live authorization and freshness after network observation. A
	// revocation or lease expiry must not survive a slow descriptor response.
	current, err := s.store.GenerationMembers(r.Context(), a.OrganizationID, a.UserID, s.nodeIdentity.NodeID(), a.Role.CanManageOrg())
	if err != nil {
		return err
	}
	// Both reads are node-id ordered; intersect in place without a second map.
	read, kept := 0, 0
	now := time.Now().UTC()
	for i := range items {
		item := &items[i]
		for read < len(current) && current[read].NodeID < item.NodeID {
			read++
		}
		if read == len(current) || current[read].NodeID != item.NodeID || !current[read].ValidUntil.After(now) {
			continue
		}
		item.DescriptorValidUntil = current[read].ValidUntil
		if item.ValidUntil != nil && !item.ValidUntil.After(now) {
			item.Readiness = contracts.CapabilityReadinessUnknown
			item.AcceptingAdmissions = false
			item.ObservedAt, item.ValidUntil = nil, nil
		}
		if kept != i {
			items[kept] = *item
		}
		kept++
	}
	items = items[:kept]
	w.Header().Set("Cache-Control", "no-store")
	return httpx.JSON(w, 200, struct {
		Items []generationCandidate `json:"items"`
	}{items})
}

func (s *Server) observeGenerationCandidate(ctx context.Context, member *discovery.NodeDescriptor, operation string) (generationCandidate, bool) {
	item := generationCandidate{NodeID: member.NodeID, State: discovery.MemberApproved,
		DescriptorValidUntil: member.ValidUntil, Operation: operation, Readiness: contracts.CapabilityReadinessUnknown}
	if cfg, ok := s.peers.Configured(member.NodeID); ok && ctx.Err() == nil {
		peerCtx, cancel := context.WithTimeout(ctx, 2*time.Second)
		defer cancel()
		profiles, status := s.peers.GenerationProfiles(peerCtx, cfg)
		if status == "observed" {
			var found *discovery.CapabilityProfile
			for i := range profiles {
				if profiles[i].Operation == operation {
					if found != nil {
						found = nil
						status = "unknown"
						break
					}
					found = &profiles[i]
				}
			}
			// A fresh complete observation without the operation is affirmative
			// evidence that the member is not a generation candidate.
			if found == nil && status == "observed" {
				return generationCandidate{}, false
			}
			if found != nil {
				item.Readiness = found.Readiness
				item.AcceptingAdmissions = found.Readiness == contracts.CapabilityReadinessReady && found.AcceptingAdmissions
				item.ObservedAt, item.ValidUntil = &found.ObservedAt, &found.ValidUntil
			}
		}
	}
	return item, member.ValidUntil.After(time.Now().UTC())
}

func (s *Server) handlePeerGenerationDescriptor(w http.ResponseWriter, r *http.Request) error {
	if len(r.URL.Query()) != 0 {
		return apierr.BadRequest("invalid_generation_descriptor", "generation descriptor has no query parameters")
	}
	profiles, status := s.capabilityProfiles(r.Context(), nil)
	generation := make([]discovery.CapabilityProfile, 0, 2)
	for _, profile := range profiles {
		if profile.Operation == "rag.answer.cited" || profile.Operation == "wiki.pages" {
			generation = append(generation, profile)
		}
	}
	w.Header().Set("Cache-Control", "no-store")
	return httpx.JSON(w, 200, map[string]any{"node_id": s.nodeIdentity.NodeID(), "profiles": generation, "capability_status": status})
}
