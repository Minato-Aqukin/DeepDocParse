package api

// Internal node-credential endpoints (ddp-node-credential/1, discovery-v1.yaml).
//
// Control is the single owner of this centre's node private key (invariant 5).
// The local corpus service never sees the seed: it asks here for one short-lived,
// single-use credential per outbound peer request, and asks here for the trust
// record of an inbound issuer. Both sides of the trust decision therefore read
// the same membership rows that administrators approve and revoke.

import (
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"log/slog"
	"net/http"
	"regexp"
	"time"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/apierr"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/discovery"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/httpx"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/store"
)

// nodeCredentialRatePerMinute bounds how many credentials the local corpus may
// obtain for one audience per minute. It is a runaway-loop brake, not a quota:
// a plan polls executions, so the bound is generous.
const nodeCredentialRatePerMinute = 3000

var nodeIDPath = regexp.MustCompile(`^[a-z0-9][a-z0-9._-]{2,63}$`)

// peerTrustReader is the one query both issuing and verifying rely on. The
// store implements it; unit tests substitute an in-memory directory that
// enforces the same states.
type peerTrustReader interface {
	PeerTrust(ctx context.Context, org, nodeID string) (*store.PeerTrustRecord, error)
}

func (s *Server) peerTrust() peerTrustReader {
	if s.trust != nil {
		return s.trust
	}
	return s.store
}

func (s *Server) clock() time.Time {
	if s.now != nil {
		return s.now()
	}
	return time.Now()
}

func (s *Server) mountNodeCredentials(mux *http.ServeMux) {
	svc := s.requireServiceCredentials
	mux.Handle("GET /internal/federation/identity", svc(httpx.Wrap(s.handleInternalNodeIdentity)))
	mux.Handle("POST /internal/federation/node-credentials", svc(httpx.Wrap(s.handleIssueNodeCredential)))
	mux.Handle("GET /internal/federation/peer-keys/{node_id}", svc(httpx.Wrap(s.handlePeerKey)))
}

func (s *Server) handleInternalNodeIdentity(w http.ResponseWriter, r *http.Request) error {
	if err := s.discoveryReady(); err != nil {
		return err
	}
	w.Header().Set("Cache-Control", "no-store")
	return httpx.JSON(w, http.StatusOK, map[string]string{
		"node_id": s.nodeIdentity.NodeID(), "public_key": s.nodeIdentity.PublicKey(),
		"key_fingerprint": s.nodeIdentity.Fingerprint(),
	})
}

type nodeCredentialRequest struct {
	AudienceNodeID string                          `json:"audience_node_id"`
	Actor          discovery.CredentialActor       `json:"actor"`
	Operation      string                          `json:"operation"`
	Constraints    discovery.CredentialConstraints `json:"constraints"`
	Request        discovery.CredentialRequest     `json:"request"`
	TTLSeconds     int64                           `json:"ttl_seconds"`
}

func credentialInvalid(message string) error {
	return apierr.BadRequest("credential_invalid", message)
}

// trustRefusal maps a membership lookup to the contract codes. Only an
// approved member is trusted; pending is not yet trust, revoked is final.
func trustRefusal(record *store.PeerTrustRecord, err error, status int) error {
	if errors.Is(err, store.ErrNotFound) || (err == nil && record.State == discovery.MemberPending) {
		return apierr.New(status, apierr.TypePermission, "node_unknown", "节点未登记或尚未批准")
	}
	if err != nil {
		return err
	}
	if record.State == discovery.MemberRevoked {
		return apierr.New(status, apierr.TypePermission, "node_revoked", "节点已被撤销")
	}
	if record.State != discovery.MemberApproved {
		return apierr.New(status, apierr.TypePermission, "node_unknown", "节点状态不可信")
	}
	return nil
}

func (s *Server) handleIssueNodeCredential(w http.ResponseWriter, r *http.Request) error {
	if err := s.discoveryReady(); err != nil {
		return err
	}
	var in nodeCredentialRequest
	if err := httpx.DecodeJSON(r, &in); err != nil {
		return credentialInvalid("签发请求不是合法的 SignRequest")
	}
	if in.TTLSeconds < 1 || in.TTLSeconds > discovery.MaxCredentialLifetimeSeconds {
		return credentialInvalid("ttl_seconds 必须为 1..120")
	}
	jti, err := discovery.NewCredentialJTI()
	if err != nil {
		return apierr.Internal("无法生成凭证 id")
	}
	now := s.clock().UTC().Unix()
	claims := discovery.CredentialClaims{
		Schema: discovery.CredentialSchema, Alg: discovery.CredentialAlg,
		IssuerNodeID: s.nodeIdentity.NodeID(), AudienceNodeID: in.AudienceNodeID,
		Actor: in.Actor, Operation: in.Operation, Constraints: in.Constraints, Request: in.Request,
		IssuedAt: now, ExpiresAt: now + in.TTLSeconds, JTI: jti,
	}
	// Validate covers audience == this node (issuer == audience), the charset,
	// the operation enum, method/step/spec rules and the lifetime bound.
	if err := claims.Validate(); err != nil {
		return credentialInvalid("签发请求违反 ddp-node-credential/1 约束")
	}
	// Control signs only for principals of its own organization: the corpus
	// cannot mint a delegation that claims to speak for another tenant.
	if in.Actor.OrganizationID != s.defaultOrg {
		return apierr.Forbidden("credential_scope_denied", "actor 不属于本节点的组织")
	}
	record, err := s.peerTrust().PeerTrust(r.Context(), s.defaultOrg, in.AudienceNodeID)
	if refusal := trustRefusal(record, err, http.StatusForbidden); refusal != nil {
		return refusal
	}
	if s.limiter != nil {
		allowed, _, limitErr := s.limiter.Allow(r.Context(), "node-credential:"+in.AudienceNodeID, nodeCredentialRatePerMinute, time.Minute)
		if limitErr != nil {
			slogWarn("rate limiter unavailable on node credential issuance, failing open", limitErr)
		} else if !allowed {
			return apierr.TooMany("rate_limited", "节点凭证签发过于频繁")
		}
	}
	token, err := s.nodeIdentity.SignCredential(claims)
	if err != nil {
		return apierr.Internal("无法签发节点凭证")
	}
	// Audit trail without the secret: the jti identifies the credential, the
	// token itself never reaches a log line.
	slog.Info("node credential issued", "audience", claims.AudienceNodeID, "operation", claims.Operation,
		"root_task_id", claims.Constraints.RootTaskID, "step_id", claims.Constraints.StepID,
		"jti", claims.JTI, "expires_at", claims.ExpiresAt)
	w.Header().Set("Cache-Control", "no-store")
	return httpx.JSON(w, http.StatusOK, map[string]any{
		"credential": token, "issuer_node_id": claims.IssuerNodeID, "jti": claims.JTI,
		"expires_at": claims.ExpiresAt,
	})
}

func (s *Server) handlePeerKey(w http.ResponseWriter, r *http.Request) error {
	if err := s.discoveryReady(); err != nil {
		return err
	}
	node := r.PathValue("node_id")
	if !nodeIDPath.MatchString(node) {
		return apierr.NotFound("node_unknown", "节点未登记")
	}
	record, err := s.peerTrust().PeerTrust(r.Context(), s.defaultOrg, node)
	if errors.Is(err, store.ErrNotFound) {
		return apierr.NotFound("node_unknown", "节点未登记")
	}
	if err != nil {
		return err
	}
	fingerprint := ""
	if raw, decodeErr := base64.StdEncoding.Strict().DecodeString(record.PublicKey); decodeErr == nil {
		sum := sha256.Sum256(raw)
		fingerprint = "sha256:" + hex.EncodeToString(sum[:])
	}
	w.Header().Set("Cache-Control", "no-store")
	return httpx.JSON(w, http.StatusOK, map[string]any{
		"node_id": record.NodeID, "state": record.State, "public_key": record.PublicKey,
		"key_fingerprint": fingerprint, "organization_id": s.defaultOrg,
		"authority_node_id": s.nodeIdentity.NodeID(), "revision": record.Revision,
	})
}
