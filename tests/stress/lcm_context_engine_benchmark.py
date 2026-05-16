"""LCM context-engine benchmark harness.

Usage:
  python tests/stress/lcm_context_engine_benchmark.py

Outputs:
  - console summary (min/median/max ms)
  - JSON report at /tmp/lcm_context_engine_bench.json
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

WT = str(Path(__file__).resolve().parents[2])


def bench(label, fn, iterations=7):
    samples = []
    for _ in range(iterations):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1000)
    samples.sort()
    return {
        "label": label,
        "iterations": iterations,
        "min_ms": samples[0],
        "median_ms": samples[len(samples) // 2],
        "max_ms": samples[-1],
    }


def seed_messages(n: int):
    out = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        out.append(
            {
                "role": role,
                "content": (
                    f"turn {i} bedtime story context moon forest "
                    f"calm prompt with unique marker id-{i}"
                ),
            }
        )
    return out


def main():
    home = tempfile.mkdtemp(prefix="hermes_lcm_bench_")
    os.environ["HERMES_HOME"] = home
    os.environ["HOME"] = home

    import sys

    sys.path.insert(0, WT)
    from plugins.context_engine.lcm.engine import LCMContextEngine

    results = []

    for n in [200, 2000, 10000]:
        engine = LCMContextEngine(context_length=20_000, threshold_percent=0.5)
        session = f"bench-{n}"
        engine.on_session_start(session)
        messages = seed_messages(n)

        results.append(bench(f"ingest_messages(n={n})", lambda: engine.ingest_messages(session, messages), iterations=3))
        results.append(bench(f"lcm_grep(n={n})", lambda: engine.handle_tool_call("lcm_grep", {"query": "moon", "limit": 20}), iterations=7))
        results.append(bench(f"lcm_expand(n={n})", lambda: engine.handle_tool_call("lcm_expand", {"query": "id-42", "before": 6, "after": 6}), iterations=7))

        if n >= 2000:
            results.append(bench(f"compress(n={n})", lambda: engine.compress(messages, focus_topic="bedtime"), iterations=3))

    print("\nLCM BENCHMARK SUMMARY")
    print("=" * 64)
    print(f"{'Benchmark':<34} {'min':>8} {'median':>8} {'max':>8}")
    for r in results:
        print(f"{r['label']:<34} {r['min_ms']:>7.1f} {r['median_ms']:>7.1f} {r['max_ms']:>7.1f}")

    out_path = "/tmp/lcm_context_engine_bench.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    print(f"\nSaved benchmark JSON: {out_path}")


if __name__ == "__main__":
    main()
