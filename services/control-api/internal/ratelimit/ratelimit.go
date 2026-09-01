// Package ratelimit 是分布式限速。
//
// **必须跨副本共享计数**，否则每个副本各限各的 —— 实际限速等于
// 配置值 × 副本数，而扩容会让这个数字悄悄变大。旧系统在这条上改过一次
// （内存计数换 Redis），这里直接继承结论。
//
// Redis 不可用时退回单进程内存计数，并且**把降级说出来**（日志 + metrics）。
// 静默退回是这个项目吃过大亏的模式。
package ratelimit

import (
	"context"
	"log/slog"
	"sync"
	"time"

	"github.com/redis/go-redis/v9"
)

type Limiter interface {
	// Allow 报告本次是否放行，以及本窗口还剩多少配额。
	Allow(ctx context.Context, key string, limit int, window time.Duration) (bool, int, error)
	Kind() string
}

// ---------------------------------------------------------------- Redis

type RedisLimiter struct {
	client *redis.Client
}

func NewRedis(client *redis.Client) *RedisLimiter { return &RedisLimiter{client: client} }

func (l *RedisLimiter) Kind() string { return "redis" }

// Allow 是固定窗口计数。
//
// 为什么不是滑动窗口：固定窗口在窗口边界上最多放行 2×limit，
// 而实现只要 INCR + EXPIRE 两个命令、无需存每次请求的时间戳。
// 对"防洪峰"这个目的，边界效应不值得用一个 sorted set 去换。
// 真需要平滑的场合（比如按 token 计费）再换令牌桶。
func (l *RedisLimiter) Allow(ctx context.Context, key string, limit int, window time.Duration) (bool, int, error) {
	if limit <= 0 {
		return true, 0, nil
	}
	bucket := time.Now().UnixNano() / int64(window)
	redisKey := "ratelimit:" + key + ":" + itoa(bucket)

	pipe := l.client.TxPipeline()
	incr := pipe.Incr(ctx, redisKey)
	// **EXPIRE 必须和 INCR 在同一个 pipeline 里**：分两次发的话，
	// 进程在中间挂掉会留下一个永不过期的计数键 —— 那个用户从此被永久限流
	pipe.Expire(ctx, redisKey, window+time.Second)
	if _, err := pipe.Exec(ctx); err != nil {
		return false, 0, err
	}
	count := int(incr.Val())
	return count <= limit, max(limit-count, 0), nil
}

// ---------------------------------------------------------------- 内存

type MemoryLimiter struct {
	mu      sync.Mutex
	buckets map[string]*bucket
}

type bucket struct {
	count int
	until time.Time
}

func NewMemory() *MemoryLimiter {
	return &MemoryLimiter{buckets: map[string]*bucket{}}
}

func (l *MemoryLimiter) Kind() string { return "memory" }

func (l *MemoryLimiter) Allow(_ context.Context, key string, limit int, window time.Duration) (bool, int, error) {
	if limit <= 0 {
		return true, 0, nil
	}
	l.mu.Lock()
	defer l.mu.Unlock()

	now := time.Now()
	b, ok := l.buckets[key]
	if !ok || now.After(b.until) {
		b = &bucket{until: now.Add(window)}
		l.buckets[key] = b
	}
	b.count++
	// 顺手清理过期桶，免得长跑进程里键无限增长
	if len(l.buckets) > 10000 {
		for k, v := range l.buckets {
			if now.After(v.until) {
				delete(l.buckets, k)
			}
		}
	}
	return b.count <= limit, max(limit-b.count, 0), nil
}

// New 按 Redis 是否可用选实现，并把选择结果**打进启动日志**。
// 多副本部署下退回内存计数是一个真实的可用性问题（限速失效），
// 必须让人在启动日志里就看见，而不是等到被刷爆才发现。
func New(ctx context.Context, redisURL string) Limiter {
	if redisURL == "" {
		slog.Warn("未配置 REDIS_URL，限速退回单进程内存计数 —— 多副本部署下实际限速 = 配置值 × 副本数")
		return NewMemory()
	}
	opt, err := redis.ParseURL(redisURL)
	if err != nil {
		slog.Error("REDIS_URL 解析失败，限速退回内存计数", "err", err)
		return NewMemory()
	}
	client := redis.NewClient(opt)
	if err := client.Ping(ctx).Err(); err != nil {
		slog.Error("Redis 连不上，限速退回内存计数", "err", err)
		return NewMemory()
	}
	slog.Info("限速使用 Redis 共享计数", "addr", opt.Addr)
	return NewRedis(client)
}

func itoa(v int64) string {
	if v == 0 {
		return "0"
	}
	var buf [20]byte
	i := len(buf)
	neg := v < 0
	if neg {
		v = -v
	}
	for v > 0 {
		i--
		buf[i] = byte('0' + v%10)
		v /= 10
	}
	if neg {
		i--
		buf[i] = '-'
	}
	return string(buf[i:])
}
