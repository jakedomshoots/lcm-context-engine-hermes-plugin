# Bulletproof Validation Pack — 2026-05-17

This report documents the extended pre-production validation run for the Hermes LCM context engine plugin.

## Scope

Validation covered:
1. Crash/restart durability and data integrity
2. Concurrent read/write soak behavior (WAL + latency)
3. Extreme growth projection (250k messages)
4. A/B retention proxy (LCM recall vs compressor compacted context)

## 1) Crash/Restart Durability

Method:
- Seeded stable data into session
- Started heavy writer process
- Killed process with `SIGKILL`
- Reopened DB and verified integrity + recall

Results:
- `PRAGMA integrity_check` => `ok`
- Stable seeded fact remained retrievable via `lcm_grep`
- Status: **PASS**

Artifact:
- `/tmp/lcm_durability_result.json`

## 2) Concurrent Read/Write Soak

Method:
- 6 concurrent reader threads (`lcm_grep`) while writer path performed periodic `compress()` up to 30k messages

Results:
- Reader call count: 2,298
- Errors: 0
- Read latency:
  - p50: 33.72 ms
  - p95: 115.62 ms
  - max: 137.63 ms
- Health remained `healthy`
- WAL peaked high during hot writes (~142.6 MB) as expected in WAL mode
- Status: **PASS**

Artifact:
- `/tmp/lcm_concurrency_result.json`

## 3) Extreme Growth Projection

Method:
- Simulated 250,000 messages
- Compaction every 2,500 messages
- Captured DB/WAL growth and compaction latency trend

Results:
- Runtime: 61.6 sec
- Final DB size: ~122.39 MB
- WAL after checkpoint: 0 MB
- Compaction latency trend:
  - early (2,500 msgs): ~40.55 ms
  - late (250,000 msgs): ~1227.21 ms
- Health remained `healthy`
- Status: **PASS**

Artifact:
- `/tmp/lcm_growth_result.json`

## 4) A/B Retention Proxy

Method:
- Synthetic transcript with 120 explicit fact markers
- LCM measured by `lcm_grep` recall
- Compressor measured by retained inline context after deterministic stub-summary compression

Results:
- LCM recall: 120/120 (100%)
- Compressor inline retention: 1/120 (0.8%)

Notes:
- This is a **retention proxy**, not a model-judged semantic quality benchmark.
- It demonstrates LCM’s lossless-recall architecture advantage.

Artifact:
- `/tmp/lcm_ab_quality_proxy.json`

## Critical scalability issue discovered + fixed

Discovered under 100k soak:
- `sqlite3.OperationalError: too many SQL variables`

Root cause:
- Single `IN (...)` bound list for summary-bound hash lookup exceeded SQLite variable limit.

Fix:
- Chunked hash lookup in `_write_summary_node()` (chunk size 800).

Outcome:
- 100k and 250k tests pass post-fix.

## 5) Final blind 3-test task-quality bake-off

Method:
- Ran three independent synthetic "blind" conversation tasks where exact target facts/anchors were known in advance.
- Compared:
  - **LCM** via retrieval tools (`lcm_grep`, `lcm_expand`) after compression
  - **Baseline compacted context** via head/tail-only inline retention
- Goal: measure practical task-memory utility, not just throughput.

Tests + results:
1. **T1 Sparse fact recall** (2,612 messages, 6 planted facts)
   - LCM recall: **6/6 (100%)**
   - Baseline recall: **0/6 (0%)**

2. **T2 Latest override correctness** (1,806 messages, 6 preference updates)
   - Expected latest value recovered by LCM: **PASS**
   - Baseline latest value recovery: **FAIL**

3. **T3 Anchor window integrity** (3,010 messages, 5 anchor/ack pairs)
   - LCM pair recall: **5/5 (100%)**
   - Baseline pair recall: **0/5 (0%)**

Aggregate:
- LCM wins: **3/3 tests**

Artifact:
- `/tmp/lcm_blind_bakeoff_3tests.json`

Caveat:
- These are deterministic/proxy task-quality tests (excellent for memory-behavior proof), not human-judged prose-quality ratings.

## Overall verdict

**GO for production rollout** with operational guardrails:
- monitor `lcm_health` (`db_size_bytes`, `wal_size_bytes`) daily initially
- trigger checkpoints when WAL remains high (e.g., >128–256MB sustained)
- keep benchmark artifacts across releases to detect regressions
