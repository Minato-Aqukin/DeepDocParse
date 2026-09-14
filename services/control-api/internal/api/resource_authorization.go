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
