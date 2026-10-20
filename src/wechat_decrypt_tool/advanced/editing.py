"""Transactional edits of decrypted copies, with conflict checked undo.

This module never opens encrypted/live WeChat databases. Sync may replace a
snapshot; undo refuses to overwrite a row changed by sync or another edit.
"""
import base64
import hashlib
import json
import re
import sqlite3
import time
import uuid
from contextlib import closing
from xml.etree import ElementTree as ET
from .store import state_dir

EDIT_KEYS = ('edit-text', 'edit-source', 'edit-time', 'edit-fields', 'edit-restore',
             'edit-fix-sender', 'edit-flip-sides', 'edit-delete-sys')
TYPES = {'text': 1, 'image': 3, 'file': 49, 'voice': 34, 'video': 43, 'emoji': 47,
         'transfer': 49, 'redpacket': 49, 'location': 48, 'link': 49, 'miniapp': 49,
         'channels': 49, 'quote': 49, 'merged': 49, 'call': 50, 'sys': 10000, 'pat': 10000}
FIELDS = {'local_id', 'server_id', 'local_type', 'sort_seq', 'real_sender_id', 'create_time',
          'status', 'upload_status', 'download_status', 'server_seq', 'origin_source',
          'source', 'message_content', 'compress_content'}


def encode(row):
    if row is None:
        return None
    return {k: {'$blob': base64.b64encode(v).decode()} if isinstance(v, bytes) else v for k, v in dict(row).items()}


def decode(row):
    return {k: base64.b64decode(v['$blob']) if isinstance(v, dict) and set(v) == {'$blob'} else v for k, v in row.items()}


def digest(rows):
    return hashlib.sha256(json.dumps(rows, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def quoted(value):
    return '"' + value.replace('"', '""') + '"'


def account_path(account):
    from ..chat_helpers import _resolve_account_dir
    from ..app_paths import get_output_databases_dir
    if not isinstance(account, str) or not account.strip():
        raise ValueError('需要明确指定账户')
    path = _resolve_account_dir(account)
    root = get_output_databases_dir().resolve()
    if path.is_symlink() or path.resolve().parent != root:
        raise ValueError('仅允许编辑 output/databases 内的解密副本')
    return path.resolve()


def target(account, username, message_id=None):
    from ..routers.chat import _resolve_decrypted_message_table
    account_dir = account_path(account)
    if not isinstance(username, str) or not username or len(username) > 256:
        raise ValueError('需要精确的联系人 username')
    expected_table = 'msg_' + hashlib.md5(username.encode()).hexdigest()
    local_id = None
    if message_id:
        if not isinstance(message_id, str):
            raise ValueError('消息标识格式不正确')
        parts = message_id.split(':')
        if len(parts) != 3 or not re.fullmatch(r'[A-Za-z0-9_-]+', parts[0]) or parts[1].lower() != expected_table or not parts[2].isdigit():
            raise ValueError('消息标识与会话不匹配')
        path, table, local_id = account_dir / (parts[0] + '.db'), parts[1], int(parts[2])
    else:
        resolved = _resolve_decrypted_message_table(account_dir, username)
        if not resolved:
            raise ValueError('该会话还没有可编辑的解密消息表，请先同步或导入')
        path, table = resolved
    if path.is_symlink():
        raise ValueError('不允许编辑符号链接数据库')
    path = path.resolve()
    if path.parent != account_dir or not path.is_file():
        raise ValueError('仅允许编辑本账户下的解密数据库')
    conn = sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    match = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND lower(name)=?", (table.lower(),)).fetchone()
    if not match:
        conn.close()
        raise ValueError('消息表不存在')
    return conn, path, match[0], local_id


def select_rows(conn, table, local_id, payload):
    clauses, values = [], []
    if local_id is not None:
        clauses.append('local_id=?')
        values.append(local_id)
    for field, operator in [('start_time', '>='), ('end_time', '<=')]:
        if payload.get(field) is not None:
            clauses.append('create_time' + operator + '?')
            values.append(int(payload[field]))
    if payload.get('feature') == 'edit-delete-sys':
        clauses.append('(local_type=10000 OR local_type=10002)')
    where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
    rows = conn.execute('SELECT * FROM ' + quoted(table) + where + ' ORDER BY local_id LIMIT 10001', values).fetchall()
    if len(rows) > 10000:
        raise ValueError('一次最多修改 10000 条，请缩小时间范围')
    return [encode(row) for row in rows]


def preview(payload):
    conn, path, table, local_id = target(payload['account'], payload['username'], payload.get('message_id'))
    try:
        rows = select_rows(conn, table, local_id, payload)
        return {'revision': digest(rows), 'count': len(rows), 'rows': rows[:20], 'database': path.stem,
                'scope': 'decrypted-copy', 'warning': '只修改分析用解密副本；重新同步可能覆盖修改，不会写入微信客户端或产生真实收付款。'}
    finally:
        conn.close()


def message_body(kind, value):
    text = str(value.get('text', ''))
    source = value.get('xml')
    if source:
        if len(source) > 262144:
            raise ValueError('XML 超过 256 KiB')
        try:
            ET.fromstring(source)
        except ET.ParseError:
            raise ValueError('消息 XML 格式不正确') from None
        return source
    if kind in ('text', 'sys'):
        if not text:
            raise ValueError('消息内容不能为空')
        return text
    if kind == 'pat':
        if not text:
            raise ValueError('需要拍一拍提示文字')
        root = ET.Element('sysmsg', {'type': 'pat'})
        ET.SubElement(ET.SubElement(root, 'pat'), 'template').text = text
        return ET.tostring(root, encoding='unicode')
    # Import actual payloads for media/payment/complex cards. Inventing server
    # resource IDs or payment metadata would create corrupt, misleading records.
    raise ValueError('该补录类型需要原消息 XML（xml 字段），媒体还需要原资源在本地可用')


def sender_id(conn, username):
    row = conn.execute('SELECT rowid FROM Name2Id WHERE user_name=?', (username,)).fetchone()
    if row is None:
        raise ValueError('发送者必须存在于该库 Name2Id 中')
    return row[0]


def apply(payload):
    feature = payload['feature']
    if feature == 'edit-restore':
        return restore(payload['account'], payload['journal_id'])
    if feature not in EDIT_KEYS and feature not in {'add-' + key for key in TYPES}:
        raise ValueError('未知编辑操作')
    if feature in ('edit-text', 'edit-source', 'edit-time', 'edit-fields') and not payload.get('message_id'):
        raise ValueError('此操作必须指定单条 message_id')
    conn, path, table, local_id = target(payload['account'], payload['username'], payload.get('message_id'))
    journal_path = state_dir() / 'edits.sqlite3'
    journal_id = uuid.uuid4().hex
    try:
        conn.execute('ATTACH DATABASE ? AS journal', (str(journal_path),))
        import os
        os.chmod(journal_path, 0o600)
        conn.execute('CREATE TABLE IF NOT EXISTS journal.edits (id TEXT PRIMARY KEY, account TEXT, database_name TEXT, table_name TEXT, before_rows TEXT, after_rows TEXT, created REAL, restored INTEGER DEFAULT 0)')
        conn.commit()
        conn.execute('BEGIN IMMEDIATE')
        before = [] if feature.startswith('add-') else select_rows(conn, table, local_id, payload)
        if not feature.startswith('add-') and (not payload.get('revision') or digest(before) != payload['revision']):
            raise ValueError('消息已变化或尚未预览，请重新预览后提交')
        cols = {row[1] for row in conn.execute('PRAGMA table_info(' + quoted(table) + ')')}
        after = []
        if feature.startswith('add-'):
            kind = feature[4:]
            body = message_body(kind, payload)
            new_id = conn.execute('SELECT COALESCE(MAX(local_id),0)+1 FROM ' + quoted(table)).fetchone()[0]
            stamp = int(payload.get('create_time') or time.time())
            if stamp <= 0:
                raise ValueError('create_time 必须是正的 Unix 秒数')
            values = {'local_id': new_id, 'server_id': 0, 'local_type': TYPES[kind], 'sort_seq': stamp * 1000,
                      'real_sender_id': sender_id(conn, payload.get('sender') or payload['username']),
                      'create_time': stamp, 'message_content': body, 'compress_content': None,
                      'status': 0, 'upload_status': 0, 'download_status': 0, 'server_seq': 0, 'origin_source': 0}
            values = {k: v for k, v in values.items() if k in cols}
            conn.execute('INSERT INTO ' + quoted(table) + ' (' + ','.join(map(quoted, values)) + ') VALUES (' + ','.join('?' for _ in values) + ')', list(values.values()))
            after = [encode(conn.execute('SELECT * FROM ' + quoted(table) + ' WHERE local_id=?', (new_id,)).fetchone())]
        else:
            mapping = payload.get('mapping', {})
            if feature == 'edit-flip-sides':
                if payload['username'].endswith('@chatroom'):
                    raise ValueError('群聊不支持互换双方')
                from ..account_identity import resolve_account_self_rowid
                left, _ = resolve_account_self_rowid(conn, path.parent)
                if left is None:
                    raise ValueError('无法识别本人在 Name2Id 中的身份')
                right = sender_id(conn, payload['username'])
                mapping = {str(left): right, str(right): left}
            for row in before:
                if feature == 'edit-delete-sys':
                    conn.execute('DELETE FROM ' + quoted(table) + ' WHERE local_id=?', (row['local_id'],))
                    continue
                changes = {}
                if feature == 'edit-text':
                    if row['local_type'] != 1:
                        raise ValueError('文本编辑仅适用于文本消息')
                    changes = {'message_content': str(payload['text']), 'compress_content': None, 'WCDB_CT_message_content': None}
                elif feature == 'edit-source':
                    xml = payload.get('xml')
                    if not isinstance(xml, str) or not xml.strip() or len(xml) > 262144:
                        raise ValueError('需要不超过 256 KiB 的原消息 XML 源码')
                    try:
                        ET.fromstring(xml)
                    except ET.ParseError:
                        raise ValueError('消息 XML 格式不正确') from None
                    changes = {'message_content': xml, 'compress_content': None, 'WCDB_CT_message_content': None}
                elif feature == 'edit-time':
                    stamp = int(payload['create_time'])
                    if stamp <= 0:
                        raise ValueError('create_time 必须是正的 Unix 秒数')
                    changes = {'create_time': stamp, 'sort_seq': stamp * 1000}
                elif feature == 'edit-fields':
                    changes = payload.get('fields', {})
                    if not isinstance(changes, dict) or not changes or set(changes) - FIELDS:
                        raise ValueError('包含不可编辑的字段')
                    if 'local_id' in changes and (isinstance(changes['local_id'], bool) or not isinstance(changes['local_id'], int) or changes['local_id'] <= 0):
                        raise ValueError('local_id 必须是正整数')
                    if 'message_content' in changes:
                        changes = dict(changes, compress_content=None, WCDB_CT_message_content=None)
                elif feature in ('edit-fix-sender', 'edit-flip-sides'):
                    if not isinstance(mapping, dict) or not mapping:
                        raise ValueError('需要 mapping，例如 {"1": 2, "2": 1}')
                    old = str(row.get('real_sender_id'))
                    if old in mapping:
                        new_sender = int(mapping[old])
                        if not conn.execute('SELECT 1 FROM Name2Id WHERE rowid=?', (new_sender,)).fetchone():
                            raise ValueError('mapping 指向不存在的发送者')
                        changes = {'real_sender_id': new_sender}
                changes = {k: v for k, v in changes.items() if k in cols}
                if changes:
                    conn.execute('UPDATE ' + quoted(table) + ' SET ' + ','.join(quoted(k) + '=?' for k in changes) + ' WHERE local_id=?', [*changes.values(), row['local_id']])
                after.append(encode(conn.execute('SELECT * FROM ' + quoted(table) + ' WHERE local_id=?', (changes.get('local_id', row['local_id']),)).fetchone()))
        conn.execute('INSERT INTO journal.edits VALUES (?,?,?,?,?,?,?,0)', (journal_id, payload['account'], path.name, table, json.dumps(before), json.dumps(after), time.time()))
        conn.commit()
        return {'status': 'applied', 'journal_id': journal_id, 'count': len(after) if feature.startswith('add-') else len(before), 'scope': 'decrypted-copy'}
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def history(account):
    path = state_dir() / 'edits.sqlite3'
    if not path.exists():
        return []
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute('SELECT id,account,database_name,table_name,created,restored FROM edits WHERE account=? ORDER BY created DESC LIMIT 100', (account,))]


def restore(account, journal_id):
    journal_path = state_dir() / 'edits.sqlite3'
    if not journal_path.exists():
        raise ValueError('没有编辑记录')
    with closing(sqlite3.connect(journal_path)) as db:
        db.row_factory = sqlite3.Row
        record = db.execute('SELECT * FROM edits WHERE id=? AND account=?', (journal_id, account)).fetchone()
    if not record or record['restored']:
        raise ValueError('编辑记录不存在或已还原')
    account_dir = account_path(account)
    original_path = account_dir / record['database_name']
    path = original_path.resolve()
    if original_path.is_symlink() or path.parent != account_dir or not path.is_file():
        raise ValueError('数据库位置已变化')
    with closing(sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=30)) as conn, conn:
        conn.row_factory = sqlite3.Row
        conn.execute('ATTACH DATABASE ? AS journal', (str(journal_path),))
        conn.execute('BEGIN IMMEDIATE')
        if conn.execute('SELECT restored FROM journal.edits WHERE id=?', (journal_id,)).fetchone()[0]:
            raise ValueError('该编辑已还原')
        before, after = json.loads(record['before_rows']), json.loads(record['after_rows'])
        wanted = {r['local_id']: r for r in after}
        identifiers = {r['local_id'] for r in before + after}
        table = quoted(record['table_name'])
        for identifier in identifiers:
            current = encode(conn.execute('SELECT * FROM ' + table + ' WHERE local_id=?', (identifier,)).fetchone())
            if current != wanted.get(identifier):
                raise ValueError('消息在编辑后又被修改或重新同步，不能直接还原')
        for identifier in identifiers:
            conn.execute('DELETE FROM ' + table + ' WHERE local_id=?', (identifier,))
        for row in before:
            values = decode(row)
            conn.execute('INSERT INTO ' + table + ' (' + ','.join(map(quoted, values)) + ') VALUES (' + ','.join('?' for _ in values) + ')', list(values.values()))
        conn.execute('UPDATE journal.edits SET restored=1 WHERE id=?', (journal_id,))
    return {'status': 'restored', 'journal_id': journal_id, 'scope': 'decrypted-copy'}
