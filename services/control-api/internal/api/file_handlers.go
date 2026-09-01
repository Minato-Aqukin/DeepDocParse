package api

import (
	"errors"
	"net/http"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/apierr"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/httpx"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/obs"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/store"
)

// handleFileByToken 是**稳定文件 URL**。
//
// model-gateway 用它下载原件，而文档身份 `doc_hash` 在没有 `doc_id` 时
// 回退成 `sha256(file_url)` —— 所以这个路径一变，幂等复用与向量索引
// 分块键全部失效（ADR #11/#12，这个项目为此踩过两次）。
//
// 实现是 **302 到短期签名 URL**，不是由本进程转发字节流：
// 转发会让每个下载都占住一个 goroutine 与一份缓冲，扩容应用等于
// 放大对象存储的带宽中转（不变式 6）。
func (s *Server) handleFileByToken(w http.ResponseWriter, r *http.Request) error {
	grant, err := s.store.FileGrantByToken(r.Context(), r.PathValue("token"))
	if err != nil {
		if errors.Is(err, store.ErrNotFound) {
			// 撤销、过期、不存在**统一 404**：区分它们等于告诉调用方
			// "这个 token 曾经有效"，而 token 本身就是凭证
			return apierr.NotFound("no_such_file", "文件不存在或凭证已失效")
		}
		return err
	}

	// service 侧只做下载，一律 attachment：
	// inline 打开一个上传上来的 text/html 就是本站同源 XSS
	disposition := "attachment"
	url, _, err := s.objects.PresignGet(r.Context(), grant.ObjectKey,
		grant.DocumentID, grant.MIME, disposition)
	if err != nil {
		return apierr.New(http.StatusBadGateway, apierr.TypeUpstream, "presign_failed",
			"签发下载地址失败").WithCause(err)
	}
	obs.PresignedURL("stable_file")

	// **签名 URL 不许被缓存**：它带着凭证，进了中间缓存就是泄露
	w.Header().Set("Cache-Control", "no-store")
	http.Redirect(w, r, url, http.StatusFound)
	return nil
}

// handleDownloadURL 给浏览器用的短期 URL。
//
// 与 `/files/{token}` **必须分开**：这条每次返回一个新 URL（因此支持
// 收紧 TTL、按 disposition 变化），而那条的路径必须永远不变。
// 拿这条去喂 model-gateway 会破坏 doc_hash 幂等 —— 契约里写了这句话。
func (s *Server) handleDownloadURL(w http.ResponseWriter, r *http.Request) error {
	actor, err := mustActor(r)
	if err != nil {
		return err
	}
	docID := r.PathValue("document_id")

	grant, err := s.store.StableGrantFor(r.Context(), actor.OrganizationID, docID, "", "")
	if err != nil || grant.ObjectKey == "" {
		// 还没有凭证说明这份文档不属于本组织，或者还没归档完
		return apierr.NotFound("no_such_document", "文档不存在或尚未归档")
	}

	disposition := r.URL.Query().Get("disposition")
	if disposition != "attachment" {
		disposition = "inline"
	}
	// inline 只对白名单类型开放。**这条不能省** ——
	// 它是"上传 text/html 就是同源 XSS"那条铁律在新架构里的落点
	if disposition == "inline" && !s.cfg.MIMEAllowed(grant.MIME) {
		disposition = "attachment"
	}

	url, expires, err := s.objects.PresignGet(r.Context(), grant.ObjectKey,
		docID, grant.MIME, disposition)
	if err != nil {
		return apierr.New(http.StatusBadGateway, apierr.TypeUpstream, "presign_failed",
			"签发下载地址失败").WithCause(err)
	}
	obs.PresignedURL("browser_download")

	w.Header().Set("Cache-Control", "no-store")
	return httpx.JSON(w, http.StatusOK, map[string]any{
		"url":        url,
		"expires_at": expires,
		// 对象存储原生支持 Range，PDF.js 因此可以只取要看的那一页 ——
		// 而不是为了看第 200 页把 200MB 全下下来
		"supports_range": true,
	})
}
