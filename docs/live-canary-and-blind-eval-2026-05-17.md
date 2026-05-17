# Live Canary + Final Blind Eval Pack — 2026-05-17

## What was added

### 1) 72h live canary scripts

- `scripts/lcm_canary_log.py`
  - Appends hourly JSONL snapshots of:
    - db size
    - wal size
    - message count
    - summary count
    - journal mode
  - Output file: `~/.hermes/logs/lcm_canary_hourly.jsonl`

- `scripts/lcm_canary_alert.sh`
  - Emits alert output only when thresholds are exceeded:
    - WAL > 256MB
    - DB > 1GB

### 2) Blind human-eval pack (60 items)

Generated locally at:
- `/tmp/lcm_human_blind_eval_pack/blind_rater_sheet.csv`
- `/tmp/lcm_human_blind_eval_pack/answer_key.json`
- `/tmp/lcm_human_blind_eval_pack/summary.json`

Quick objective proxy tally from generated pack:
- LCM exact recovery: 60/60
- Baseline exact recovery: 0/60

## Cron jobs configured

- `lcm-canary-hourly-log-72h`
  - no_agent script job
  - schedule: every 60m
  - repeat: 72
  - deliver: local

- `lcm-canary-alert-72h`
  - no_agent script job
  - schedule: every 15m
  - repeat: 288
  - deliver: origin (chat alerts only on threshold breach)

## Rollback command

If needed:

```bash
hermes config set context.engine compressor
```

Verify:

```bash
hermes config get context.engine
```
