// Package identity 定义"这次请求是谁发的"，并负责把它下发给上游服务。
//
// corpus-api 与 model-gateway **不自己验用户凭据**：它们只信任本服务
// 通过内部头下发的这组信息。这是旧系统"service 不感知用户"那条边界的
// 直接继承 —— 变的只是从单一 SERVICE_TOKEN 升级成带 actor 上下文的服务身份。
//
// # 头绝不能来自客户端
//
// 入口**无条件剥掉**客户端传来的同名头再填自己的。少了这一步，
// 任何人都能发一个 `X-DDP-Role: admin` 把自己变成管理员，而且
// 上游会完全正常地接受它 —— 没有报错、没有日志异常，就是权限没了。
// `httpx.StripInboundIdentity` 做这件事，`TestClientCannotForgeIdentity` 钉着它。
package identity

import (
	"context"
	"net/http"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/rbac"
)

// 内部头。**改名字要同时改 corpus-api 的 ddp_corpus/deps.py**，
// 那边有一份同样的常量，靠 scripts/check_internal_headers.py 对拍。
const (
	HeaderRequestID    = "X-Request-Id"
	HeaderTraceParent  = "traceparent"
	HeaderService      = "X-DDP-Service"
	HeaderOrganization = "X-DDP-Organization"
	HeaderActor        = "X-DDP-Actor"
	HeaderActorKind    = "X-DDP-Actor-Kind"
	HeaderRole         = "X-DDP-Role"
	HeaderAPIKeyID     = "X-DDP-Api-Key"
	HeaderIdempotency  = "Idempotency-Key"
)

// Inbound 列出所有**必须从客户端请求里剥掉**的头。
// 新增一个内部头就往这里加一行 —— 漏加等于开了一个提权后门。
var Inbound = []string{
	HeaderService, HeaderOrganization, HeaderActor,
	HeaderActorKind, HeaderRole, HeaderAPIKeyID,
}

type Kind string

const (
	KindUser    Kind = "user"
	KindAPIKey  Kind = "api_key"
	KindService Kind = "service"
)

// Actor 是一次请求的调用者。
type Actor struct {
	Kind            Kind
	ID              string // user id 或 api key id
	UserID          string // api_key 时是它的所有者
	OrganizationID  string
	Role            rbac.Role
	APIKeyID        string
	Scopes          []rbac.Scope
	RateLimitPerMin int
	RequestID       string
}

// HasScope 报告这个 actor 能不能调某个平面。
// **用户会话不受 scope 限制**（scope 是 API key 的最小权限机制），
// 但仍然受角色限制 —— 两者是不同的维度。
func (a *Actor) HasScope(s rbac.Scope) bool {
	if a.Kind != KindAPIKey {
		return true
	}
	for _, have := range a.Scopes {
		if have == s {
			return true
		}
	}
	return false
}

type ctxKey struct{}

func With(ctx context.Context, a *Actor) context.Context {
	return context.WithValue(ctx, ctxKey{}, a)
}

// From 取出当前 actor。没有则返回 nil —— 调用方必须处理，
// 不要在这里造一个"匿名 actor"兜底：那会让忘记挂鉴权中间件的路由
// 表现为"以匿名身份正常工作"，而不是当场 500。
func From(ctx context.Context) *Actor {
	a, _ := ctx.Value(ctxKey{}).(*Actor)
	return a
}

// Apply 把 actor 上下文写进转发给上游的请求。
func (a *Actor) Apply(r *http.Request, serviceName string) {
	r.Header.Set(HeaderService, serviceName)
	r.Header.Set(HeaderOrganization, a.OrganizationID)
	r.Header.Set(HeaderActor, a.ID)
	r.Header.Set(HeaderActorKind, string(a.Kind))
	r.Header.Set(HeaderRole, string(a.Role))
	if a.APIKeyID != "" {
		r.Header.Set(HeaderAPIKeyID, a.APIKeyID)
	}
	if a.RequestID != "" {
		r.Header.Set(HeaderRequestID, a.RequestID)
	}
}
