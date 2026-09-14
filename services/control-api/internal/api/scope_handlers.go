package api

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/apierr"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/auth"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/contracts"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/discovery"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/httpx"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/identity"
)

func (s *Server) mountScopes(mux *http.ServeMux) {
	mux.Handle("POST /api/v1/federation/scopes", s.discoveryAuth(httpx.Wrap(s.handleScopeCreate)))
	mux.Handle("GET /api/v1/federation/scopes/{scope_id}", s.discoveryAuth(httpx.Wrap(s.handleScopeRead)))
	mux.Handle("GET /api/v1/federation/scopes/{scope_id}/targets", s.discoveryAuth(httpx.Wrap(s.handleScopeTargets)))
}
func (s *Server) handleScopeCreate(w http.ResponseWriter, r *http.Request) error {
	if err := s.discoveryReady(); err != nil {
		return err
	}
	a, err := mustActor(r)
	if err != nil {
		return err
	}
	opts := discovery.ScopeOptions{PageSize: 50, MaxMembers: 1000, MaxDiscoveryRequests: 64, MaxRemoteMembers: 32, TTLSeconds: 900}
	if err = httpx.DecodeJSON(r, &opts); err != nil {
		return err
	}
	if opts.Validate() != nil {
		return apierr.BadRequest("invalid_scope_options", "operation 与合法分页、范围预算及有效期必填")
	}
	callerScope := discovery.ScopeHash(a)
	if opts.MemberSnapshotID == "" {
		snap, e := s.store.CreateMemberSnapshot(r.Context(), a.OrganizationID, a.UserID, callerScope, s.nodeIdentity.NodeID(), a.Role.CanManageOrg(), opts.PageSize, time.Duration(opts.TTLSeconds)*time.Second)
		if e != nil {
			return discoveryError(e)
		}
		opts.MemberSnapshotID = snap.ID
	} else {
		// Authenticate the referenced member snapshot before contacting any producer.
		_, e := s.store.MemberSnapshotPage(r.Context(), a.OrganizationID, a.UserID, callerScope, opts.MemberSnapshotID, "", a.Role.CanManageOrg())
		if e != nil {
			return discoveryError(e)
		}
	}
	scopeID := auth.NewID()
	catalog := s.collectScopeCatalog(r.Context(), a, scopeID, opts.MaxMembers)
	remote := discovery.RemoteExpansion{}
	if s.peers != nil {
		// The frozen snapshot is reread through the same authorization overlay the
		// caller would see; revoked and hidden members are never contacted.
		members, complete, e := s.store.AuthorizedSnapshotMembers(r.Context(), a.OrganizationID, a.UserID, callerScope, opts.MemberSnapshotID, a.Role.CanManageOrg())
		if e != nil {
			return discoveryError(e)
		}
		if complete {
			budget := opts.MaxMembers - len(catalog.Collections)
			if budget < 0 {
				budget = 0
			}
			ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
			remote = discovery.ExpandScope(ctx, s.peers, discovery.ExpansionInput{
				Members: members, LocalNodeID: s.nodeIdentity.NodeID(), Operation: opts.Operation,
				MaxTargets: budget, MaxRequests: opts.MaxDiscoveryRequests, MaxNodes: opts.MaxRemoteMembers,
				Now: time.Now().UTC(),
			})
			cancel()
		}
	}
	out, err := s.store.CreateExpandedScope(r.Context(), a.OrganizationID, a.UserID, callerScope, s.nodeIdentity.NodeID(), scopeID, a.Role.CanManageOrg(), opts, catalog, remote)
	if err != nil {
		return discoveryError(err)
	}
	s.store.Audit(r.Context(), a.OrganizationID, a.ID, string(a.Kind), "scope.create", scopeID, a.RequestID, map[string]any{"manifest_digest": out.Manifest.ManifestDigest, "enumeration_state": out.Manifest.EnumerationState, "known_targets": out.TotalTargets})
	w.Header().Set("Cache-Control", "no-store")
	return httpx.JSON(w, 201, out)
}
func (s *Server) handleScopeRead(w http.ResponseWriter, r *http.Request) error {
	a, err := mustActor(r)
	if err != nil {
		return err
	}
	out, err := s.store.ScopeManifest(r.Context(), a.OrganizationID, discovery.ScopeHash(a), r.PathValue("scope_id"))
	if err != nil {
		return discoveryError(err)
	}
	w.Header().Set("Cache-Control", "no-store")
	return httpx.JSON(w, 200, out)
}
func (s *Server) handleScopeTargets(w http.ResponseWriter, r *http.Request) error {
	if err := s.discoveryReady(); err != nil {
		return err
	}
	a, err := mustActor(r)
	if err != nil {
		return err
	}
	callerScope := discovery.ScopeHash(a)
	scopeID := r.PathValue("scope_id")
	out, err := s.store.ScopeTargets(r.Context(), a.OrganizationID, a.UserID, callerScope, scopeID, r.URL.Query().Get("cursor"), s.nodeIdentity.NodeID(), a.Role.CanManageOrg())
	if err != nil {
		return discoveryError(err)
	}
	// The immutable manifest survives revocation. New use of its local targets
	// revalidates exactly its original catalog; no fresh catalog is spliced in.
	if len(out.Targets) > 0 && !out.Expired {
		source, e := s.store.ScopeCatalogSource(r.Context(), a.OrganizationID, callerScope, scopeID)
		if e == nil && !source.Revoked {
			ctx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
			page, code := s.scopeCatalogPage(ctx, a, scopeID, source.SnapshotID, source.TerminalCursor, 0)
			cancel()
			state := ""
			if code == "catalog_snapshot_invalid" {
				revokedIDs := []string{}
				if page != nil {
					revokedIDs = page.RevokedCollectionIDs
					if revokedIDs == nil {
						revokedIDs = []string{}
					}
				}
				if err = s.store.RevokeScopeCollections(r.Context(), a.OrganizationID, callerScope, scopeID, revokedIDs); err != nil {
					return err
				}
				out, err = s.store.ScopeTargets(r.Context(), a.OrganizationID, a.UserID, callerScope, scopeID, r.URL.Query().Get("cursor"), s.nodeIdentity.NodeID(), a.Role.CanManageOrg())
				if err != nil {
					return err
				}
			} else if code != "" || page == nil || !page.Complete || len(page.Collections) != 0 || page.NextCursor != nil || page.SnapshotID != source.SnapshotID || page.ScopeID != scopeID || page.CallerScopeHash != callerScope || page.OriginNodeID != source.NodeID {
				state = string(contracts.CoverageTargetStateUnreachable)
			}
			if state != "" {
				for i := range out.Targets {
					if out.Targets[i].TargetKey.OriginNodeID == source.NodeID {
						out.Targets[i].State = state
					}
				}
			}
		} else if e != nil {
			for i := range out.Targets {
				out.Targets[i].State = string(contracts.CoverageTargetStateUnreachable)
			}
		}
	}
	w.Header().Set("Cache-Control", "no-store")
	return httpx.JSON(w, 200, out)
}

type scopeCatalogPage struct {
	SnapshotID       string    `json:"snapshot_id"`
	ScopeID          string    `json:"scope_id"`
	CallerScopeHash  string    `json:"caller_scope_hash"`
	OriginNodeID     string    `json:"origin_node_id"`
	RegistryRevision int64     `json:"registry_revision"`
	CreatedAt        time.Time `json:"created_at"`
	ValidUntil       time.Time `json:"valid_until"`
	FirstCursor      string    `json:"first_cursor"`
	TerminalCursor   string    `json:"terminal_cursor"`
	Total            *int      `json:"total"`
	Collections      []struct {
		CollectionID string `json:"collection_id"`
		OriginNodeID string `json:"origin_node_id"`
	} `json:"collections"`
	NextCursor           *string  `json:"next_cursor"`
	Complete             bool     `json:"complete"`
	RevokedCollectionIDs []string `json:"revoked_collection_ids,omitempty"`
}

func (s *Server) scopeCatalogPage(ctx context.Context, a *identity.Actor, scopeID, snapshotID, cursor string, limit int) (*scopeCatalogPage, string) {
	if s.corpus == nil {
		return nil, "unknown"
	}
	q := url.Values{"scope_id": {scopeID}}
	if snapshotID != "" {
		q.Set("snapshot_id", snapshotID)
	}
	if cursor != "" {
		q.Set("cursor", cursor)
	}
	if limit > 0 {
		q.Set("limit", strconv.Itoa(limit))
	}
	req, err := http.NewRequestWithContext(ctx, "GET", strings.TrimRight(s.cfg.CorpusURL, "/")+"/internal/federation/collections?"+q.Encode(), nil)
	if err != nil {
		return nil, "unknown"
	}
	service := controlServiceActor()
	service.OrganizationID = a.OrganizationID
	service.Apply(req, "control-api")
	req.Header.Set("Authorization", "Bearer "+s.cfg.ServiceToken)
	req.Header.Set(identity.HeaderCallerActor, a.ID)
	req.Header.Set(identity.HeaderCallerKind, string(a.Kind))
	req.Header.Set(identity.HeaderCallerRole, string(a.Role))
	req.Header.Set(identity.HeaderCallerUser, a.UserID)
	req.Header.Set(identity.HeaderCallerScope, discovery.ScopeHash(a))
	req.Header.Set(identity.HeaderAuthorityNode, s.nodeIdentity.NodeID())
	client := &http.Client{Transport: s.corpus.Transport(), CheckRedirect: func(_ *http.Request, _ []*http.Request) error { return http.ErrUseLastResponse }}
	resp, err := client.Do(req)
	if err != nil {
		if errors.Is(err, context.DeadlineExceeded) {
			return nil, "timeout"
		}
		return nil, "unknown"
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, (1<<20)+1))
	if err != nil || len(body) > 1<<20 {
		return nil, "unknown"
	}
	if resp.StatusCode != 200 {
		var failure struct {
			Error struct {
				Code string `json:"code"`
			} `json:"error"`
			Detail struct {
				Code string `json:"code"`
			} `json:"detail"`
		}
		_ = json.Unmarshal(body, &failure)
		code := failure.Error.Code
		if code == "" {
			code = failure.Detail.Code
		}
		if resp.StatusCode == 410 && (code == "catalog_snapshot_invalid" || code == "catalog_snapshot_expired") {
			var invalid scopeCatalogPage
			_ = json.Unmarshal(body, &invalid)
			return &invalid, code
		}
		if resp.StatusCode == 409 {
			return nil, "catalog_snapshot_changed"
		}
		if resp.StatusCode == 401 || resp.StatusCode == 403 {
			return nil, "denied"
		}
		return nil, "unknown"
	}
	var page scopeCatalogPage
	if json.Unmarshal(body, &page) != nil {
		return nil, "unknown"
	}
	return &page, ""
}

func (s *Server) collectScopeCatalog(parent context.Context, a *identity.Actor, scopeID string, budget int) discovery.CollectionCatalog {
	ctx, cancel := context.WithTimeout(parent, 10*time.Second)
	defer cancel()
	out := discovery.CollectionCatalog{FailureReason: "unknown"}
	snapshotID, cursor := "", ""
	seen := map[string]bool{}
	collections := map[string]bool{}
	var first *scopeCatalogPage
	for requests := 0; requests < (budget+99)/100+2; requests++ {
		if first != nil && seen[cursor] {
			return out
		}
		limit := 0
		if first == nil {
			limit = 100
		}
		page, code := s.scopeCatalogPage(ctx, a, scopeID, snapshotID, cursor, limit)
		if code != "" {
			if code == "timeout" || code == "denied" {
				out.FailureReason = code
			}
			return out
		}
		if page.SnapshotID == "" || page.ScopeID != scopeID || page.CallerScopeHash != discovery.ScopeHash(a) || page.OriginNodeID != s.nodeIdentity.NodeID() || page.RegistryRevision < 1 || page.CreatedAt.IsZero() || !page.ValidUntil.After(time.Now()) || !page.ValidUntil.After(page.CreatedAt) || page.FirstCursor == "" || page.TerminalCursor == "" || page.Total == nil || *page.Total < 0 || *page.Total > 10000 {
			return out
		}
		if first == nil {
			first = page
			snapshotID = page.SnapshotID
			cursor = page.FirstCursor
			out = discovery.CollectionCatalog{SnapshotID: snapshotID, ScopeID: scopeID, CallerScopeHash: page.CallerScopeHash, NodeID: page.OriginNodeID, Revision: page.RegistryRevision, FetchedAt: page.CreatedAt, ValidUntil: page.ValidUntil, TerminalCursor: page.TerminalCursor, Collections: []string{}, FailureReason: "unknown"}
		} else if page.SnapshotID != first.SnapshotID || page.RegistryRevision != first.RegistryRevision || !page.CreatedAt.Equal(first.CreatedAt) || !page.ValidUntil.Equal(first.ValidUntil) || page.FirstCursor != first.FirstCursor || page.TerminalCursor != first.TerminalCursor || *page.Total != *first.Total {
			return out
		}
		if seen[cursor] {
			return out
		}
		seen[cursor] = true
		if cursor == page.TerminalCursor {
			// A terminal marker is not evidence that every promised unique member was
			// received. Never seal a truncated or internally inconsistent catalog.
			out.Complete = page.Complete && page.NextCursor == nil && len(page.Collections) == 0 && len(collections) == *first.Total
			if out.Complete {
				out.FailureReason = ""
			}
			return out
		}
		if page.Complete || page.NextCursor == nil || *page.NextCursor == "" || len(page.Collections) > 100 {
			return out
		}
		if len(page.Collections) == 0 && (len(collections) != *first.Total || *page.NextCursor != page.TerminalCursor) {
			// An empty data page is only a step towards the separate terminal page
			// when every declared collection was already observed. Emptiness alone
			// never proves completion, and an early empty page is a truncation.
			return out
		}
		for _, collection := range page.Collections {
			if collection.CollectionID == "" || collection.OriginNodeID != out.NodeID {
				return out
			}
			if collections[collection.CollectionID] {
				continue
			}
			if len(collections) >= *first.Total {
				return out
			}
			if len(out.Collections) >= budget {
				out.FailureReason = "budget_exhausted"
				return out
			}
			collections[collection.CollectionID] = true
			out.Collections = append(out.Collections, collection.CollectionID)
		}
		cursor = *page.NextCursor
	}
	out.FailureReason = "budget_exhausted"
	return out
}
