# LCM Stress Test Results — 2026-05-16

## Purpose
Validate DB/WAL growth and compaction latency under day-scale and extreme-scale simulated context loads before enabling `context.engine: lcm` in production.

## Environment
- Host: Ubuntu 24.04 arm64
- Engine: `plugins/context_engine/lcm/engine.py`
- SQLite mode: `journal_mode=WAL`, `synchronous=NORMAL`

## Test 1 — 24h-equivalent load
Simulation profile:
- 12,500 messages
- compaction every 250 messages (50 compactions)

Key results:
- Final DB size (pre-checkpoint): ~7.754 MB
- Final WAL size (pre-checkpoint): ~5.395 MB
- Final DB size (post-checkpoint): ~8.188 MB
- Final WAL size (post-checkpoint): 0 MB
- Final `lcm_health.status`: `healthy`
- Compression latency growth:
  - early (~250 msgs): ~9.47 ms
  - late (~12,500 msgs): ~45.75 ms

Artifact:
- `/tmp/lcm_day_sim_results.json`

## Test 2 — Extreme soak
Simulation profile:
- 100,000 messages
- compaction every 1,000 messages (100 compactions)

Initial run discovered a scalability bug:
- `sqlite3.OperationalError: too many SQL variables`
- Root cause: single `IN (...)` query exceeded SQLite variable limit during summary bound lookup.

Fix applied:
- Chunked `message_hash` lookup in `_write_summary_node()` with chunk size 800.

Post-fix results:
- Final DB size (pre-checkpoint): ~60.098 MB
- Final WAL size (pre-checkpoint): ~8.852 MB
- Final DB size (post-checkpoint): ~60.102 MB
- Final WAL size (post-checkpoint): 0 MB
- Final `lcm_health.status`: `healthy`
- Compression latency growth:
  - early (~1,000 msgs): ~17.64 ms
  - late (~100,000 msgs): ~456.78 ms

Artifact:
- `/tmp/lcm_big_sim_results.json`

## Conclusion
- WAL growth is expected under sustained writes and truncates cleanly on checkpoint.
- DB growth is linear and reasonable for persisted full text at this scale.
- LCM remains operationally healthy at 100k messages after chunking fix.
- Config switch to `context.engine: lcm` is justified with monitoring.

## Recommended guardrails
- Monitor `lcm_health` daily during early rollout (`db_size_bytes`, `wal_size_bytes`).
- Trigger checkpoint when WAL exceeds an operational threshold (e.g., 128–256 MB sustained).
- Keep benchmark artifact snapshots across releases to catch regressions.
