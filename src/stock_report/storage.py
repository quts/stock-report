"""Local run-scoped evidence persistence and canonical hashes."""
import hashlib
import json
import sqlite3
from pathlib import Path


def canonical(value):
    if hasattr(value, 'model_dump'):
        value = value.model_dump(mode='json')
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def write_json(path, value):
    path = Path(path)
    if hasattr(value, 'model_dump'):
        value = value.model_dump(mode='json')
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')


def read_json(path):
    return json.loads(Path(path).read_text())


def save_evidence(path, evidence):
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE IF NOT EXISTS evidence (evidence_id TEXT PRIMARY KEY, entity_id TEXT NOT NULL, payload TEXT NOT NULL)')
        db.executemany('INSERT INTO evidence VALUES (?, ?, ?)',
                       [(e.evidence_id, e.entity_id, canonical(e)) for e in evidence])
