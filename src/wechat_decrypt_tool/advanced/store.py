"""Private durable configuration and action receipts. Never return secrets."""
import json
import os
import sqlite3
from pathlib import Path
from contextlib import contextmanager
from ..app_paths import get_data_dir


def state_dir():
    path = Path(get_data_dir()) / 'advanced'
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    return path


@contextmanager
def connect():
    path = state_dir() / 'state.sqlite3'
    db = sqlite3.connect(path, timeout=30)
    os.chmod(path, 0o600)
    db.row_factory = sqlite3.Row
    db.execute('CREATE TABLE IF NOT EXISTS config (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS receipts (id TEXT PRIMARY KEY, feature TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL, result TEXT NOT NULL, created REAL NOT NULL)')
    db.execute("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, value TEXT NOT NULL, status TEXT NOT NULL, cursor INTEGER NOT NULL DEFAULT 0, due REAL NOT NULL, result TEXT NOT NULL DEFAULT '')")
    try:
        with db:
            yield db
    finally:
        db.close()


def config():
    with connect() as db:
        row = db.execute('SELECT value FROM config WHERE id=1').fetchone()
    value = json.loads(row[0]) if row else {}
    value['origin'] = os.environ.get('WEHARBOR_URL', value.get('origin', 'http://127.0.0.1:8766')).rstrip('/')
    value['token'] = os.environ.get('WEHARBOR_API_TOKEN', value.get('token', ''))
    if not value['token']:
        token_file = Path(os.environ.get('WEHARBOR_TOKEN_FILE', '/config/.config/weharbor/workspace.token'))
        if token_file.is_file():
            value['token'] = token_file.read_text().strip()
    value.setdefault('harness_url', '/assistant/')
    value.setdefault('tool_bindings', {})
    return value


def public_config():
    value = config()
    value['token_configured'] = bool(value.pop('token', ''))
    return value


def save_config(value):
    # Omitted/empty token preserves an existing credential. Explicit clear_token clears it.
    with connect() as db:
        row = db.execute('SELECT value FROM config WHERE id=1').fetchone()
    current = json.loads(row[0]) if row else {}
    for key in ('origin', 'harness_url', 'tool_bindings'):
        if key in value:
            current[key] = value[key]
    if value.get('token'):
        current['token'] = value['token']
    if value.get('clear_token'):
        current['token'] = ''
    with connect() as db:
        db.execute('INSERT OR REPLACE INTO config VALUES (1, ?)', (json.dumps(current),))
    return public_config()
