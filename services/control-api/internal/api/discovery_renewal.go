package api

import (
	"context"
	"log/slog"
	"net/http"
	"sync"
	"time"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/discovery"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/store"
)

func (s *Server) renewDiscoveryLeases(ctx context.Context) {
	if s.nodeIdentity == nil || s.store == nil {
		return
	}
	interval := s.cfg.DiscoveryRenewalInterval
	if interval <= 0 {
		interval = time.Minute
	}
	client := discovery.NewLeaseClient()
	defer client.CloseIdleConnections()
	ticker := time.NewTicker(min(time.Second, interval/4))
	defer ticker.Stop()
	for {
		for ctx.Err() == nil && s.renewDiscoveryBatch(ctx, client, interval) == 32 {
			// A full batch is not the end of the due queue.
		}
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
	}
}

func (s *Server) renewDiscoveryBatch(ctx context.Context, client *http.Client, interval time.Duration) int {
	claims, err := s.store.ClaimDiscoveryRenewals(ctx)
	if err != nil {
		if ctx.Err() == nil {
			slog.Error("discovery renewal claim failed", "error", err)
		}
		return 0
	}
	jobs := make(chan store.DiscoveryRenewal)
	var workers sync.WaitGroup
	for range min(4, len(claims)) {
		workers.Add(1)
		go func() {
			defer workers.Done()
			for claim := range jobs {
				peerCtx, cancel := context.WithTimeout(ctx, 2*time.Second)
				descriptor, failure := discovery.FetchLease(peerCtx, client, claim.Descriptor, claim.PublicKey, s.nodeIdentity.NodeID())
				cancel()
				applied, err := s.store.CompleteDiscoveryRenewal(ctx, claim, descriptor, failure, interval)
				outcome := "renewed"
				if failure != nil {
					outcome = "failed"
				}
				if !applied {
					outcome = "superseded"
				}
				if err != nil {
					outcome = "store_failed"
				}
				slog.Info("discovery renewal", "node_id", claim.Descriptor.NodeID, "organization_id", claim.OrganizationID, "outcome", outcome, "error", failure, "store_error", err)
			}
		}()
	}
	for _, claim := range claims {
		select {
		case jobs <- claim:
		case <-ctx.Done():
			close(jobs)
			workers.Wait()
			return 0
		}
	}
	close(jobs)
	workers.Wait()
	return len(claims)
}
