# PR Draft — LCM Context Engine (Production Candidate)

## Title
feat(context-engine): add production-candidate LCM engine with DAG metadata, FTS recall, health tooling, and benchmark harness

## Summary
This PR introduces a production-candidate `lcm` context engine plugin as an alternative to the built-in `compressor`.

It provides lossless persistence, structured recall tools, hierarchical summary metadata, and operational introspection with reproducible performance benchmarking.

## What’s Included

### Core engine (`plugins/context_engine/lcm/`)
- SQLite-backed lossless message persistence
- Compression marker strategy (non-destructive middle replacement)
- Hierarchical summary metadata:
  - `parent_summary_id`
  - `depth`
  - `covered_message_count`
- FTS5-backed `lcm_grep` with safe `LIKE` fallback
- Operational `lcm_health` tool
- WAL + `synchronous=NORMAL` pragmas for production behavior
- Safer argument validation for tool params

### Tools
- `lcm_grep`
- `lcm_describe`
- `lcm_expand`
- `lcm_stats`
- `lcm_health`

### Tests
- Extended `tests/plugins/test_lcm_context_engine.py`
- Regression suite coverage via:
  - `tests/agent/test_context_engine.py`
  - `tests/hermes_cli/test_plugins_cmd.py`

### Benchmarking
- Added `tests/stress/lcm_context_engine_benchmark.py`
- Produces console min/median/max and JSON artifact `/tmp/lcm_context_engine_bench.json`

### Docs
- `website/docs/developer-guide/context-engine-plugin.md`
- `website/docs/user-guide/configuration.md` (migration/rollback section)
- `website/docs/guides/lcm-ab-evaluation.md`
- `plugins/context_engine/lcm/README.md`

## Validation Receipts

```bash
python -m pytest tests/plugins/test_lcm_context_engine.py tests/agent/test_context_engine.py tests/hermes_cli/test_plugins_cmd.py -q
# Result: 87 passed

python tests/stress/lcm_context_engine_benchmark.py
# Example medians observed on ARM64 dev host:
# - lcm_grep(n=10000): 5.3ms
# - lcm_expand(n=10000): 1.7ms
# - compress(n=10000): 31.3ms
```

## Migration Notes

Enable LCM:

```yaml
context:
  engine: "lcm"
```

Rollback:

```yaml
context:
  engine: "compressor"
```

## Known Limitation / Follow-up

- Summary generation is currently deterministic extractive text, not model-generated abstractive summaries.
- Future follow-up: model-backed summaries and multi-parent DAG rollups.

## Review Checklist

- [ ] Functional behavior is correct under long sessions
- [ ] Recall tools restore expected details after compaction
- [ ] Benchmark deltas are acceptable
- [ ] Docs are clear for migration/rollback
