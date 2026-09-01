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
)

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
