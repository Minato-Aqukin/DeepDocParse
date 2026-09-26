package api

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/apierr"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/identity"
)

// Authorization remains in the corpus domain. Never query its tables from Go,
// or cache an allow result across a publication/ownership change.
type fileAccess struct {
	DocumentID string `json:"document_id"`
	ResourceID string `json:"resource_id"`
	ObjectKey  string `json:"object_key"`
	Filename   string `json:"filename"`
	MIME       string `json:"mime"`
}

func (s *Server) documentAccess(ctx context.Context, actor *identity.Actor, documentID, resourceID string) (*fileAccess, error) {
	if actor == nil || documentID == "" || strings.ContainsAny(documentID, "/\\?#") {
		return nil, apierr.NotFound("no_such_document", "文档不存在或无权访问")
	}
	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	target := strings.TrimRight(s.cfg.CorpusURL, "/") + "/internal/file-access/" + url.PathEscape(documentID)
	if resourceID != "" {
		target += "?resource_id=" + url.QueryEscape(resourceID)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, target, nil)
	if err != nil {
		return nil, err
	}
	actor.Apply(req, "control-api")
	req.Header.Set("Authorization", "Bearer "+s.cfg.ServiceToken)
	client := &http.Client{Transport: s.corpus.Transport(), CheckRedirect: func(_ *http.Request, _ []*http.Request) error {
		return http.ErrUseLastResponse
	}}
	resp, err := client.Do(req)
	if err != nil {
		return nil, apierr.New(502, apierr.TypeUpstream, "authorization_unavailable", "资源权限服务不可用")
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusOK {
		var access fileAccess
		if err := json.NewDecoder(io.LimitReader(resp.Body, 65536)).Decode(&access); err != nil || access.DocumentID != documentID || access.ObjectKey == "" || (resourceID != "" && access.ResourceID != resourceID) {
			return nil, apierr.New(502, apierr.TypeUpstream, "authorization_invalid", "资源权限服务响应无效")
		}
		return &access, nil
	}
	if resp.StatusCode == 409 {
		return nil, apierr.New(409, apierr.TypeInvalidRequest, "resource_context_required", "请选择具体资源")
	}
	if resp.StatusCode == 403 || resp.StatusCode == 404 {
		return nil, apierr.NotFound("no_such_document", "文档不存在或无权访问")
	}
	return nil, apierr.New(502, apierr.TypeUpstream, "authorization_unavailable", "资源权限服务不可用")
}

// uploadTargetAdmission asks the corpus domain whether actor may append a byte
// version to resourceID (live, same organization, owned by the uploader, not
// withdrawn, and — when the declared digest is known — not already a version).
// It runs before any storage is allocated. The DocumentSubmitted consumer
// re-runs the same predicate, so an allow here is admission, not a reservation:
// a later deletion surfaces as a rejected ingest, never a silent new resource.
func (s *Server) uploadTargetAdmission(ctx context.Context, actor *identity.Actor, resourceID string, sha256 *string) error {
	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	target := strings.TrimRight(s.cfg.CorpusURL, "/") + "/internal/upload-target/" + url.PathEscape(resourceID)
	if sha256 != nil {
		target += "?sha256=" + url.QueryEscape(*sha256)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, target, nil)
	if err != nil {
		return err
	}
	actor.Apply(req, "control-api")
	req.Header.Set("Authorization", "Bearer "+s.cfg.ServiceToken)
	client := &http.Client{Transport: s.corpus.Transport(), CheckRedirect: func(_ *http.Request, _ []*http.Request) error {
		return http.ErrUseLastResponse
	}}
	resp, err := client.Do(req)
	if err != nil {
		return apierr.New(502, apierr.TypeUpstream, "upload_target_unavailable", "无法确认目标资源，请稍后重试")
	}
	defer resp.Body.Close()
	var body struct {
		ResourceID string `json:"resource_id"`
		Error      struct {
			Code string `json:"code"`
		} `json:"error"`
	}
	decodeErr := json.NewDecoder(io.LimitReader(resp.Body, 65536)).Decode(&body)
	switch {
	case resp.StatusCode == http.StatusOK && decodeErr == nil && body.ResourceID == resourceID:
		return nil
	case resp.StatusCode == http.StatusNotFound && body.Error.Code == "resource_not_found":
		return apierr.NotFound("resource_not_found", "目标资源不存在、已撤回或无权追加版本")
	case resp.StatusCode == http.StatusConflict && body.Error.Code == "resource_version_exists":
		return apierr.New(http.StatusConflict, apierr.TypeInvalidRequest, "resource_version_exists", "该内容已是目标资源的一个版本")
	}
	return apierr.New(502, apierr.TypeUpstream, "upload_target_unavailable", "无法确认目标资源，请稍后重试")
}
