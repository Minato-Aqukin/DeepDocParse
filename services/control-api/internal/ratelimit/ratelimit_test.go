package ratelimit

import (
	"context"
	"sync"
	"testing"
	"time"
)

// 旧系统对限速有三条用例（web 的 test_ops.py）。合仓时这一层从 Python
// 搬到 Go，用例没跟过来 —— 这个文件把它们补回来，并按新实现补了几条。

func TestAllowsUpToLimitThenRejects(t *testing.T) {
	l := NewMemory()
	ctx := context.Background()

	for i := 1; i <= 3; i++ {
		ok, remaining, err := l.Allow(ctx, "key:a", 3, time.Minute)
		if err != nil {
			t.Fatalf("第 %d 次出错：%v", i, err)
		}
		if !ok {
			t.Fatalf("第 %d 次就被拒了，限额是 3", i)
		}
		if want := 3 - i; remaining != want {
			t.Errorf("第 %d 次剩余 = %d，应为 %d", i, remaining, want)
		}
	}

	ok, remaining, _ := l.Allow(ctx, "key:a", 3, time.Minute)
	if ok {
		t.Error("第 4 次应当被拒")
	}
	// 超额后剩余不能是负数 —— 它会原样进 X-RateLimit-Remaining 响应头
	if remaining != 0 {
		t.Errorf("超额后剩余 = %d，应当夹到 0", remaining)
	}
}

func TestCountsAreIsolatedPerKey(t *testing.T) {
	l := NewMemory()
	ctx := context.Background()

	for i := 0; i < 3; i++ {
		l.Allow(ctx, "key:a", 3, time.Minute)
	}
	// 把 a 打满不该影响 b。**这是"限速按 key 计"那句话的全部含义** ——
	// 漏了它就变成一个用户能把整个部署限住
	if ok, _, _ := l.Allow(ctx, "key:b", 3, time.Minute); !ok {
		t.Error("另一把 key 被 a 的计数连累了")
	}
}

func TestWindowResets(t *testing.T) {
	l := NewMemory()
	ctx := context.Background()
	window := 40 * time.Millisecond

	for i := 0; i < 2; i++ {
		l.Allow(ctx, "key:c", 2, window)
	}
	if ok, _, _ := l.Allow(ctx, "key:c", 2, window); ok {
		t.Fatal("窗口内第 3 次应当被拒")
	}

	time.Sleep(window + 20*time.Millisecond)
	ok, remaining, _ := l.Allow(ctx, "key:c", 2, window)
	if !ok {
		t.Error("窗口过了还在拒 —— 计数没重置")
	}
	if remaining != 1 {
		t.Errorf("新窗口第一次剩余 = %d，应为 1", remaining)
	}
}

// limit <= 0 是"不限速"，不是"一次都不许"。
// 反过来的话，配置里漏填 rate_limit_per_min 会让那把 key 一个请求都发不出去。
func TestNonPositiveLimitMeansUnlimited(t *testing.T) {
	l := NewMemory()
	for _, limit := range []int{0, -1} {
		for i := 0; i < 5; i++ {
			if ok, _, _ := l.Allow(context.Background(), "key:d", limit, time.Minute); !ok {
				t.Fatalf("limit=%d 应当不限速，第 %d 次却被拒", limit, i+1)
			}
		}
	}
}

// 限速器被所有请求并发调用。计数错了不会报错，只会变成"限额比配置的松"。
func TestConcurrentAllowCountsExactly(t *testing.T) {
	l := NewMemory()
	const total = 200
	const limit = 50

	var wg sync.WaitGroup
	var mu sync.Mutex
	allowed := 0
	for i := 0; i < total; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if ok, _, _ := l.Allow(context.Background(), "key:race", limit, time.Minute); ok {
				mu.Lock()
				allowed++
				mu.Unlock()
			}
		}()
	}
	wg.Wait()

	if allowed != limit {
		t.Errorf("并发 %d 次、限额 %d，实际放行 %d 次", total, limit, allowed)
	}
}

func TestKindIsReportedSoTheDegradationIsVisible(t *testing.T) {
	// 退回内存计数是一个真实的可用性问题（多副本下限速失效）。
	// Kind() 是它唯一的可观测出口 —— 启动日志与 /metrics 都用它
	if got := NewMemory().Kind(); got != "memory" {
		t.Errorf("Kind() = %q，应为 %q", got, "memory")
	}
}

func TestNewWithoutRedisFallsBackToMemory(t *testing.T) {
	if got := New(context.Background(), "").Kind(); got != "memory" {
		t.Errorf("没配 REDIS_URL 时应当退回内存计数，得到 %q", got)
	}
}
