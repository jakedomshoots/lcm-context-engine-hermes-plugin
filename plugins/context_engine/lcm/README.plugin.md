# LCM Context Engine (MVP)

Hermes context-engine plugin name: `lcm`

## Enable

Set in `~/.hermes/config.yaml`:

```yaml
context:
  engine: lcm
```

Then start a new session (`/reset` or restart Hermes).

## Tools

- `lcm_grep` — search persisted session history (FTS5 when available; LIKE fallback)
- `lcm_describe` — fetch full message by id/query
- `lcm_expand` — fetch surrounding window around a hit
- `lcm_stats` — show persisted message/summary counts and DAG depth for a session
- `lcm_health` — operational snapshot (db size, WAL size, pragmas, FTS availability)

## Storage

Default SQLite path:

- `~/.hermes/context/lcm/lcm.db`

Optional env overrides:

- `HERMES_LCM_DB_PATH` — custom SQLite path
- `HERMES_LCM_THRESHOLD_PERCENT` — compression threshold (default `0.75`)
- `HERMES_LCM_HEAD_KEEP` — messages kept at head during compression (default `2`)
- `HERMES_LCM_TAIL_KEEP` — messages kept at tail during compression (default `16`)

## Notes

Phase-2 status:
- Lossless persistence stays intact
- Summary nodes now include DAG metadata (`parent_summary_id`, `depth`, `covered_message_count`)
- `lcm_grep` uses FTS5 full-text search when available, with safe LIKE fallback

Still pending for a future phase: model-generated summaries (current summary text is deterministic extractive snippets).

## Benchmarking

Run the reproducible stress harness:

```bash
python tests/stress/lcm_context_engine_benchmark.py
```

It emits:
- console latency summary (min/median/max)
- JSON artifact: `/tmp/lcm_context_engine_bench.json`

Recommended production gate before publishing:
- no test regressions (`pytest` suite green)
- benchmark median latency does not regress >2x against previous baseline for:
  - `lcm_grep(n=10000)`
  - `lcm_expand(n=10000)`
  - `compress(n=2000+)`
