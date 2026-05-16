# LCM Context Engine for Hermes Agent

Standalone distribution of the **LCM (Lossless Context Management) context engine plugin** for Hermes Agent.

This repo is intentionally **not** a full Hermes clone. It contains only the plugin and supporting artifacts needed to install, evaluate, and iterate on LCM.

## What this plugin provides

- Lossless conversation persistence to SQLite
- Non-destructive compression marker flow
- Recall tools:
  - `lcm_grep` (FTS5 search with LIKE fallback)
  - `lcm_describe`
  - `lcm_expand`
  - `lcm_stats`
  - `lcm_health`
- DAG-style summary metadata:
  - `parent_summary_id`
  - `depth`
  - `covered_message_count`
- Operational defaults:
  - WAL mode
  - `synchronous=NORMAL`

## Repo layout

- `plugins/context_engine/lcm/` — plugin code + manifest
- `tests/plugins/test_lcm_context_engine.py` — plugin test coverage (Hermes test harness context)
- `tests/stress/lcm_context_engine_benchmark.py` — benchmark harness
- `docs/lcm-upstream-pr-draft.md` — upstream PR draft template/reference

## Install into an existing Hermes checkout

Assume your Hermes source is at `~/hermes-agent`.

```bash
# from this repo root
cp -R plugins/context_engine/lcm ~/hermes-agent/plugins/context_engine/
```

Then enable in Hermes config:

```yaml
context:
  engine: "lcm"
```

Restart Hermes or `/reset`.

## Optional LCM tuning

```yaml
context:
  engine: "lcm"
  lcm:
    threshold_percent: 0.75
    head_keep: 2
    tail_keep: 16
    db_path: "~/.hermes/context/lcm/lcm.db"
```

Env override equivalents:
- `HERMES_LCM_THRESHOLD_PERCENT`
- `HERMES_LCM_HEAD_KEEP`
- `HERMES_LCM_TAIL_KEEP`
- `HERMES_LCM_DB_PATH`

## Benchmark

```bash
python tests/stress/lcm_context_engine_benchmark.py
```

Writes JSON output to `/tmp/lcm_context_engine_bench.json`.

## Stress-test findings

Latest published soak report:
- `docs/stress-test-results-2026-05-16.md`

Highlights:
- 12.5k-message day-equivalent simulation: healthy, WAL truncates cleanly after checkpoint
- 100k-message extreme soak: healthy post-fix, DB ~60MB, WAL truncates to 0 after checkpoint
- scalability fix included for SQLite variable-limit overflow during summary bound lookup

## Notes

- This plugin targets Hermes Agent’s `ContextEngine` interface.
- The included tests/bench scripts assume Hermes source layout/imports are available.
- Summary generation is currently deterministic extractive text (not model-generated abstractive summaries yet).

## License

MIT (same intent as Hermes upstream). Add/replace LICENSE if your distribution policy differs.
