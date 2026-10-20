"""Compare complete local contact snapshots without inferring deletion causes."""
import asyncio
import json
import time
import sqlite3
from types import SimpleNamespace
from .store import connect

LOCK = asyncio.Lock()
FIELDS = ('username', 'displayName', 'remark', 'nickname', 'alias', 'gender',
          'signature', 'type', 'country', 'province', 'city', 'region')


def tables(db):
    db.execute('CREATE TABLE IF NOT EXISTS contact_snapshots (account TEXT PRIMARY KEY, value TEXT NOT NULL, captured REAL NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS contact_changes (id INTEGER PRIMARY KEY, account TEXT NOT NULL, kind TEXT NOT NULL, username TEXT NOT NULL, before_value TEXT, after_value TEXT, captured REAL NOT NULL)')


def record(account, contacts, total):
    if not isinstance(contacts, list) or total != len(contacts):
        raise ValueError('联系人快照不完整，本次不记录变化')
    current = {}
    for item in contacts:
        if not isinstance(item, dict) or not isinstance(item.get('username'), str) or not item['username'] or item['username'] in current:
            raise ValueError('联系人快照存在无效或重复身份，本次不记录变化')
        current[item['username']] = {key: item.get(key) for key in FIELDS}
    now = time.time()
    with connect() as db:
        tables(db)
        db.execute('BEGIN IMMEDIATE')
        previous = db.execute('SELECT value FROM contact_snapshots WHERE account=?', (account,)).fetchone()
        # First observation is a baseline, never a burst of newly added contacts.
        if previous:
            before = json.loads(previous[0])
            for username in sorted(before.keys() | current.keys()):
                old, new = before.get(username), current.get(username)
                if old == new:
                    continue
                kind = 'added' if old is None else 'not_in_current_list' if new is None else 'changed'
                db.execute('INSERT INTO contact_changes(account,kind,username,before_value,after_value,captured) VALUES (?,?,?,?,?,?)',
                    (account, kind, username, json.dumps(old, ensure_ascii=False), json.dumps(new, ensure_ascii=False), now))
        db.execute('INSERT OR REPLACE INTO contact_snapshots VALUES (?,?,?)',
            (account, json.dumps(current, ensure_ascii=False), now))
    return history(account)


def history(account):
    with connect() as db:
        tables(db)
        snapshot = db.execute('SELECT captured FROM contact_snapshots WHERE account=?', (account,)).fetchone()
        rows = db.execute('SELECT * FROM contact_changes WHERE account=? ORDER BY id DESC LIMIT 200', (account,)).fetchall()
    return {'account': account, 'captured': snapshot[0] if snapshot else None,
            'changes': [dict(row) | {'before': json.loads(row['before_value']), 'after': json.loads(row['after_value'])} for row in rows],
            'scope': 'local-contact-snapshots', 'warning': '不在当前列表中不代表对方删除或拉黑了你。'}


async def capture(app, account):
    from ..routers.advanced import local_read
    async with LOCK:
        data = await local_read(SimpleNamespace(app=app), 'contacts', {'account': account})
        if data.get('status') != 'success':
            raise ValueError('联系人读取未成功，本次不记录变化')
        return await asyncio.to_thread(record, account, data.get('contacts'), data.get('total'))


async def scheduler(app):
    from ..routers.advanced import local_read
    while True:
        try:
            data = await local_read(SimpleNamespace(app=app), 'accounts', {})
            for account in data.get('accounts', []):
                try:
                    await capture(app, account)
                except (ValueError, OSError, sqlite3.Error):
                    pass  # Missing or failed snapshots never imply removal.
        except (ValueError, OSError, sqlite3.Error):
            pass
        await asyncio.sleep(3600)
