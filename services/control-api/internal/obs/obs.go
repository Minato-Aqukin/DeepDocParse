// Package obs 是可观测性：Prometheus 指标与统一日志字段。
//
// 统一字段（§13）：request_id / trace_id / organization_id / actor_id /
// api_key_id / document_id / parse_job_id / task_id / engine / model / degraded。
//
// **日志里绝不能出现**：原文全文、JWT、API key、SERVICE_TOKEN、
// 预签名 URL 的查询串、上传内容。`TestLogsDoNotLeakSecrets` 钉着这件事。
package obs

import (
	"net/http"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
	"github.com/prometheus/client_golang/prometheus/promhttp"
)

var (
	requests = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "ddp_control_requests_total",
		Help: "control-api 处理的请求数",
	}, []string{"method", "route", "status"})

	latency = promauto.NewHistogramVec(prometheus.HistogramOpts{
		Name: "ddp_control_request_duration_seconds",
		Help: "control-api 请求耗时",
		// 桶按这个服务的真实形状选：绝大多数是几毫秒的鉴权+转发，
		// 但代理 SSE 时会有几十秒的长连接。默认桶在两端都测不准
		Buckets: []float64{.005, .01, .025, .05, .1, .25, .5, 1, 2.5, 5, 10, 30, 60},
	}, []string{"method", "route"})

	uploadBytes = promauto.NewCounter(prometheus.CounterOpts{
		Name: "ddp_control_upload_bytes_total",
		Help: "直传完成并通过校验的字节数",
	})

	uploadFinalizeFailures = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "ddp_control_upload_finalize_failures_total",
		Help: "finalize 失败次数，按原因",
	}, []string{"reason"})

	outboxBacklog = promauto.NewGauge(prometheus.GaugeOpts{
		Name: "ddp_control_outbox_backlog",
		Help: "未投递的 outbox 事件数",
	})

	outboxOldest = promauto.NewGauge(prometheus.GaugeOpts{
		Name: "ddp_control_outbox_oldest_seconds",
		Help: "最老一条未投递事件的年龄。**比积压数更能说明问题** —— " +
			"积压 100 可能只是刚来一批，最老一条 20 分钟没投出去才是故障",
	})

	presigned = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "ddp_control_presigned_urls_total",
		Help: "签发的预签名 URL 数，按用途",
	}, []string{"purpose"})

	// per-phase 延迟：discovery / input / queue /
	// retrieval / generation / verify / delivery 七个阶段各一条时间线。
	// per-request 直方图只回答"这次请求慢"，这组回答"慢在哪一段" ——
	// delivery 慢与 retrieval 慢在唯一可用的图上原来长得一模一样。
	// 阶段名是契约外的运维词汇（枚举只管用户可见状态），来源就是这里，
	// ObservePhase 做参数校验：不在名单里的 phase 记成 "other"，不炸基数。
	phaseLatency = promauto.NewHistogramVec(prometheus.HistogramOpts{
		Name:    "ddp_control_phase_duration_seconds",
		Help:    "control-api 分阶段耗时",
		Buckets: []float64{.005, .01, .025, .05, .1, .25, .5, 1, 2.5, 5, 10, 30, 60},
	}, []string{"phase"})
)

// controlPhases 是 ObservePhase 认的阶段名单。新增阶段先改这里 ——
// label 基数只随这里涨，不随调用方传什么涨。
var controlPhases = map[string]bool{
	"discovery": true, "input": true, "queue": true, "retrieval": true,
	"generation": true, "verify": true, "delivery": true,
}

// ObservePhase 记一次分阶段耗时。未知 phase 记 "other" 不丢弃 ——
// 调用方拼错阶段名时，错误本身可见（other 突然涨），而不是静默消失。
func ObservePhase(phase string, d time.Duration) {
	if !controlPhases[phase] {
		phase = "other"
	}
	phaseLatency.WithLabelValues(phase).Observe(d.Seconds())
}

// FederationFields 是联邦生命周期的统一日志字段：
// 与 store.FederationCorrelation 同名同义 —— 查审计的人与查日志的人
// 用同一组 key（root_task_id / step_id / probe_id / admission_id /
// attempt / coverage_ref / delivery_state）。空指针字段直接省略，
// 日志里不出现空键。
type FederationFields struct {
	RootTaskID    *string
	StepID        *string
	ProbeID       *string
	AdmissionID   *string
	Attempt       *int
	CoverageRef   *string
	DeliveryState *string
}

// Fields 压成 slog 的交错键值对：slog.Info("...", obs.Federation(f).Fields()...)。
func (f FederationFields) Fields() []any {
	var out []any
	if f.RootTaskID != nil {
		out = append(out, "root_task_id", *f.RootTaskID)
	}
	if f.StepID != nil {
		out = append(out, "step_id", *f.StepID)
	}
	if f.ProbeID != nil {
		out = append(out, "probe_id", *f.ProbeID)
	}
	if f.AdmissionID != nil {
		out = append(out, "admission_id", *f.AdmissionID)
	}
	if f.Attempt != nil {
		out = append(out, "attempt", *f.Attempt)
	}
	if f.CoverageRef != nil {
		out = append(out, "coverage_ref", *f.CoverageRef)
	}
	if f.DeliveryState != nil {
		out = append(out, "delivery_state", *f.DeliveryState)
	}
	return out
}

func Observe(method, route string, status int, d time.Duration) {
	requests.WithLabelValues(method, route, statusClass(status)).Inc()
	latency.WithLabelValues(method, route).Observe(d.Seconds())
}

// statusClass 只记 2xx/4xx/5xx 而不是具体码：
// 具体码会让时间序列基数乘以状态码的种类数，而排查时看的是类别。
func statusClass(status int) string {
	switch {
	case status < 300:
		return "2xx"
	case status < 400:
		return "3xx"
	case status < 500:
		return "4xx"
	default:
		return "5xx"
	}
}

func UploadCompleted(bytes int64) { uploadBytes.Add(float64(bytes)) }
func UploadFailed(reason string)  { uploadFinalizeFailures.WithLabelValues(reason).Inc() }
func OutboxState(count int, oldest time.Duration) {
	outboxBacklog.Set(float64(count))
	outboxOldest.Set(oldest.Seconds())
}
func PresignedURL(purpose string) { presigned.WithLabelValues(purpose).Inc() }

func MetricsHandler() http.Handler { return promhttp.Handler() }
