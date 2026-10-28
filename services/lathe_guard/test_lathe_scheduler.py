"""
test_lathe_scheduler.py — Win32 1.0ms Timer & Core Affinity Jitter Benchmark
The Crafters Hub | The Cabinet AI Automation
"""

import time
import numpy as np
from lathe_scheduler import MASK_SAFETY_CORES, win32_realtime_context


def main():
    iterations = 500
    target_sleep_s = 0.002  # 2.0 ms target sleep
    jitters_ms = []

    with win32_realtime_context(affinity_mask=MASK_SAFETY_CORES, time_critical=True):
        for _ in range(iterations):
            t0 = time.perf_counter_ns()
            time.sleep(target_sleep_s)
            elapsed_ms = (time.perf_counter_ns() - t0) / 1_000_000.0
            jitters_ms.append(abs(elapsed_ms - (target_sleep_s * 1000.0)))

    p50 = float(np.percentile(jitters_ms, 50))
    p95 = float(np.percentile(jitters_ms, 95))
    p99 = float(np.percentile(jitters_ms, 99))

    print(f"[SCHEDULER BENCHMARK] 2.0ms Sleep Jitter -> P50: {p50:.3f}ms | P95: {p95:.3f}ms | P99: {p99:.3f}ms")
    assert p95 < 1.0, f"Win32 sleep jitter P95 ({p95:.3f}ms) exceeded 1.0ms threshold!"
    print("[PASS] Module 2 Win32 Real-Time Scheduler Verified.")


def test_win32_scheduler_jitter_benchmark():
    """Run the Win32 1.0ms timer & core affinity jitter benchmark under pytest."""
    main()


if __name__ == "__main__":
    main()
