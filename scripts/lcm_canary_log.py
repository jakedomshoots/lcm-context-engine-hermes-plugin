#!/usr/bin/env python3
import json
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

DB = Path.home()/'.hermes'/'context'/'lcm'/'lcm.db'
OUT = Path.home()/'.hermes'/'logs'/'lcm_canary_hourly.jsonl'
OUT.parent.mkdir(parents=True, exist_ok=True)

row = {
    'ts': datetime.now(timezone.utc).isoformat(),
    'db_path': str(DB),
    'exists': DB.exists(),
}

if DB.exists():
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute('PRAGMA journal_mode;')
    row['journal_mode'] = cur.fetchone()[0]
    cur.execute('PRAGMA page_size;')
    page_size = int(cur.fetchone()[0])
    cur.execute('PRAGMA page_count;')
    page_count = int(cur.fetchone()[0])
    row['db_size_bytes'] = page_size * page_count
    wal = DB.with_suffix(DB.suffix + '-wal')
    row['wal_size_bytes'] = wal.stat().st_size if wal.exists() else 0
    try:
        cur.execute('SELECT COUNT(*) FROM messages;')
        row['messages'] = int(cur.fetchone()[0])
    except Exception:
        row['messages'] = None
    try:
        cur.execute('SELECT COUNT(*) FROM summaries;')
        row['summaries'] = int(cur.fetchone()[0])
    except Exception:
        row['summaries'] = None
    conn.close()
else:
    row.update({'journal_mode': None, 'db_size_bytes': 0, 'wal_size_bytes': 0, 'messages': 0, 'summaries': 0})

with OUT.open('a', encoding='utf-8') as f:
    f.write(json.dumps(row) + '\n')

print(json.dumps({'ok': True, 'logged': row, 'log_file': str(OUT)}))
