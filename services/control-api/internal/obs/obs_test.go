package obs

import (
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus/testutil"
)

// 七个阶段各自独立计时；拼错的阶段名进 "other"（可见、不丢、不炸基数）。
func TestObservePhaseSplitsAndFoldsUnknown(t *testing.T) {
	for _, p := range []string{"discovery", "input", "queue", "retrieval", "generation", "verify", "delivery"} {
		ObservePhase(p, time.Millisecond)
		if got := testutil.CollectAndCount(phaseLatency, "ddp_control_phase_duration_seconds"); got == 0 {
			t.Fatalf("phase histogram not exposed after observing %q", p)
		}
	}
	// "other" 系列平时不存在 —— 拼错阶段名时它第一次出现（+1），
	// 之后再拼错不再涨（复用同一系列，基数不炸）。这正是折叠的含义。
	series := func() int { return testutil.CollectAndCount(phaseLatency, "ddp_control_phase_duration_seconds") }
	ObservePhase("bogus-phase", time.Millisecond)
	afterFirst := series()
	if afterFirst != 8 {
		t.Fatalf("seven phases + other = 8 series, got %d", afterFirst)
	}
	ObservePhase("another-bogus", time.Millisecond)
	if again := series(); again != afterFirst {
		t.Fatalf("unknown phases must share one other series: %d -> %d", afterFirst, again)
	}
}

// FederationFields: 空指针字段直接省略，日志里不出现空键。
func TestFederationFieldsOmitsEmpty(t *testing.T) {
	root := "r"
	f := FederationFields{RootTaskID: &root}
	fields := f.Fields()
	if len(fields) != 2 || fields[0] != "root_task_id" || fields[1] != "r" {
		t.Fatalf("federation fields wrong: %v", fields)
	}
	if len((FederationFields{}).Fields()) != 0 {
		t.Fatalf("empty correlation must emit no log keys")
	}
}
