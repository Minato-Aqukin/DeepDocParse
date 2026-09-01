// Package apierr 是全服务统一的错误体。
//
// 形状与模型网关一致（OpenAI 风格），这样 SDK 只需要一套解析逻辑：
//
//	{"error": {"message": "...", "type": "...", "code": "..."}}
//
// **不要在 handler 里直接 http.Error**：那会吐出一个纯文本体，
// 客户端的错误解析当场失效，而这种不一致只在出错路径上暴露 —— 也就是
// 最不容易被测到的那条路径。
package apierr

import (
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
)

// Type 是错误的大类，与网关侧取值一致。
const (
	TypeInvalidRequest = "invalid_request_error"
	TypeAuth           = "authentication_error"
	TypePermission     = "permission_error"
	TypeRateLimit      = "rate_limit_error"
	TypeQuota          = "quota_error"
	TypeUpstream       = "upstream_error"
	TypeInternal       = "internal_error"
)

// Error 是一个可以直接写给客户端的错误。
type Error struct {
	Status  int    `json:"-"`
	Message string `json:"message"`
	Type    string `json:"type"`
	Code    string `json:"code,omitempty"`

	// cause 只进日志，**绝不进响应体** —— 内部错误细节是信息泄露面。
	cause error
}

func (e *Error) Error() string { return e.Message }
func (e *Error) Unwrap() error { return e.cause }

// WithCause 挂一个只写日志的底层错误。
func (e *Error) WithCause(err error) *Error {
	clone := *e
	clone.cause = err
	return &clone
}

func New(status int, typ, code, message string) *Error {
	return &Error{Status: status, Type: typ, Code: code, Message: message}
}

func BadRequest(code, msg string) *Error {
	return New(http.StatusBadRequest, TypeInvalidRequest, code, msg)
}
func Unauthorized(code, msg string) *Error { return New(http.StatusUnauthorized, TypeAuth, code, msg) }
func Forbidden(code, msg string) *Error    { return New(http.StatusForbidden, TypePermission, code, msg) }
func NotFound(code, msg string) *Error {
	return New(http.StatusNotFound, TypeInvalidRequest, code, msg)
}
func Conflict(code, msg string) *Error {
	return New(http.StatusConflict, TypeInvalidRequest, code, msg)
}
func TooMany(code, msg string) *Error {
	return New(http.StatusTooManyRequests, TypeRateLimit, code, msg)
}
func PaymentRequired(code, msg string) *Error {
	return New(http.StatusPaymentRequired, TypeQuota, code, msg)
}
func Internal(msg string) *Error {
	return New(http.StatusInternalServerError, TypeInternal, "internal", msg)
}

type envelope struct {
	Error struct {
		Message string `json:"message"`
		Type    string `json:"type"`
		Code    string `json:"code,omitempty"`
	} `json:"error"`
}

// Write 把 err 写成统一错误体。非 *Error 一律当成 500 —— 并且
// **响应体里不带原始 error 文本**：那可能包含连接串、SQL、内网地址。
func Write(w http.ResponseWriter, r *http.Request, err error) {
	var e *Error
	if !errors.As(err, &e) {
		e = Internal("internal server error").WithCause(err)
	}

	if e.Status >= 500 {
		slog.ErrorContext(r.Context(), "request failed",
			"status", e.Status, "code", e.Code, "path", r.URL.Path,
			"request_id", r.Header.Get("X-Request-Id"), "err", e.cause)
	} else if e.cause != nil {
		slog.InfoContext(r.Context(), "request rejected",
			"status", e.Status, "code", e.Code, "path", r.URL.Path, "err", e.cause)
	}

	var body envelope
	body.Error.Message = e.Message
	body.Error.Type = e.Type
	body.Error.Code = e.Code

	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(e.Status)
	_ = json.NewEncoder(w).Encode(body)
}
