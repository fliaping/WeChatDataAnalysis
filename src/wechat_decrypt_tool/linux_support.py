"""Linux source adapter. Keys remain local; only verified results are published."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

from .app_paths import get_output_databases_dir
from .database_filters import should_skip_source_database
from .image_key_resolver import scan_v2_templates, verify_key_pair
from .key_store import upsert_account_keys_in_store, get_account_keys_from_store, _atomic_write_json
from .wechat_decrypt import WeChatDatabaseDecryptor

_LOCK = threading.Lock()

def local_sources(config_path: str | Path) -> dict[str, Path]:
    roots = [Path.home() / 'xwechat_files', Path.home() / 'Documents/xwechat_files']
    try:
        configured = json.loads(Path(config_path).read_text()).get('dbPath')
        if configured:
            roots.insert(0, Path(configured).expanduser())
    except (OSError, ValueError):
        pass
    sources = {}
    for root in roots:
        if not root.is_dir():
            continue
        candidates = [root] if (root / 'db_storage').is_dir() else list(root.iterdir())
        for account in candidates:
            if (account / 'db_storage').is_dir():
                sources[account.name] = (account / 'db_storage').resolve()
    return sources

def available_accounts(config_path: str | Path) -> list[dict]:
    try:
        found = {item['account']: item for item in weflow_accounts(config_path)}
    except (OSError, ValueError):
        found = {}
    for account, source in local_sources(config_path).items():
        if account in found:
            continue
        keys = get_account_keys_from_store(account)
        if keys.get('db_key'):
            found[account] = {'account': account, 'source': source, 'key': keys['db_key'],
                'xor': keys.get('image_xor_key'), 'xor_format': 'hex', 'aes': keys.get('image_aes_key')}
    return list(found.values())

def capture_linux_key(account: str, config_path: str | Path) -> dict:
    """Explicit opt-in GDB capture; never log out WeChat or return a secret."""
    from .vendor.wcdb_key_tool import tool
    source = local_sources(config_path).get(account)
    if source is None:
        raise ValueError('Unknown local account')
    if not shutil.which('gdb'):
        raise ValueError('GDB is not installed')
    # subprocess.run already kills its own child on timeout. Upstream's global
    # killall(gdb) cleanup could stop unrelated debuggers and is never invoked.
    tool._cleanup_tracer = lambda pid: None
    helper = os.environ.get('WECHAT_TOOL_CAPTURE_HELPER')
    if helper:
        import subprocess
        result = subprocess.run(['sudo', '-n', helper], input=json.dumps({'mode':'database'}),
            text=True, capture_output=True, timeout=130, check=True)
        key = json.loads(result.stdout)['key']
    else:
        key = tool.capture_passphrase(timeout=120)
    result = sync_account({'account': account, 'source': source, 'key': key}, force=True)
    if not result['changed']:
        raise ValueError('Captured key did not authenticate a local database')
    return result

def weflow_accounts(config_path: str | Path) -> list[dict]:
    """Read the owner's existing WeFlow settings without returning secrets via HTTP."""
    data = json.loads(Path(config_path).expanduser().read_text())
    accounts = dict(data.get('wxidConfigs') or {})
    current = str(data.get('myWxid') or '')
    if current and current not in accounts:
        accounts[current] = data
    roots = [Path.home() / 'xwechat_files', Path.home() / 'Documents/xwechat_files']
    configured = data.get('dbPath')
    if configured:
        roots.insert(0, Path(configured).expanduser())
    found = []
    for account, values in accounts.items():
        if not isinstance(values, dict):
            continue
        for root in roots:
            candidates = [root / account / 'db_storage', root / 'db_storage' if root.name == account else root / account / 'db_storage']
            source = next((p.resolve() for p in candidates if p.is_dir()), None)
            if source and values.get('decryptKey'):
                found.append({'account': account, 'source': source, 'key': values['decryptKey'],
                              'xor': values.get('imageXorKey', data.get('imageXorKey')),
                              'aes': values.get('imageAesKey', data.get('imageAesKey'))})
                break
    return found

def _signature(path: Path) -> tuple:
    def stamp(p):
        try:
            s = p.stat()
            return s.st_ino, s.st_size, s.st_mtime_ns
        except FileNotFoundError:
            return None
    return stamp(path), stamp(Path(str(path) + '-wal'))

def _snapshot(source: Path, target: Path) -> None:
    # Never decrypt a DB and WAL from different writes. Retry the entire pair.
    for _ in range(4):
        before = _signature(source)
        shutil.copyfile(source, target)
        wal = Path(str(source) + '-wal')
        out_wal = Path(str(target) + '-wal')
        out_wal.unlink(missing_ok=True)
        if before[1] is not None:
            shutil.copyfile(wal, out_wal)
        if before == _signature(source):
            return
    raise RuntimeError('Database is busy; no stable DB/WAL snapshot was obtained')

def sync_account(item: dict, *, force: bool = False) -> dict:
    account, source = str(item['account']), Path(item['source']).resolve()
    if Path(account).name != account or account in ('.', '..') or not source.is_dir():
        raise ValueError('Invalid account or source directory')
    output = get_output_databases_dir() / account
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(output, 0o700)
    report_path = output / '_linux_sync.json'
    decryptor = WeChatDatabaseDecryptor(str(item['key']))
    changed, skipped, failures = [], [], []
    paths = sorted(p for p in source.rglob('*.db') if not should_skip_source_database(p.name))
    names = [p.name for p in paths]
    if len(names) != len(set(names)):
        raise ValueError('Duplicate database basenames cannot be safely imported')
    with _LOCK, tempfile.TemporaryDirectory(prefix='.linux-', dir=output) as temporary:
        previous = json.loads(report_path.read_text()) if report_path.exists() else {}
        signatures = previous.get('signatures', {})
        os.chmod(temporary, 0o700)
        for original in paths:
            name = original.name
            signature = json.loads(json.dumps(_signature(original)))
            if not force and signatures.get(name) == signature and (output / name).exists():
                skipped.append(name)
                continue
            try:
                snapshot = Path(temporary) / name
                _snapshot(original, snapshot)
                candidate = Path(temporary) / ('plain-' + name)
                if not decryptor.decrypt_database(str(snapshot), str(candidate)):
                    raise ValueError('Database authentication or decryption failed')
                with sqlite3.connect(f'file:{candidate}?mode=ro', uri=True) as db:
                    if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                        raise ValueError('SQLite integrity check failed')
                os.chmod(candidate, 0o600)
                os.replace(candidate, output / name)
                signatures[name] = signature
                changed.append(name)
            except Exception as exc:
                failures.append({'database': name, 'error': type(exc).__name__})
        _atomic_write_json(output / '_source.json', {'wxid_dir': str(source.parent),
            'db_storage_path': str(source), 'import_mode': 'linux_snapshot', 'platform': 'linux'})
        _atomic_write_json(report_path, {
            'signatures': signatures,
            'revision': int(previous.get('revision', 0)) + bool(changed),
            'last_attempt_at': time.time(),
            'last_success_at': previous.get('last_success_at', 0) if failures else time.time(),
            'failed_count': len(failures),
        })
    if changed:
        upsert_account_keys_in_store(account, db_key=str(item['key']),
            db_key_source_wxid_dir=str(source.parent), db_key_source_db_storage_path=str(source))
    image_verified = False
    if item.get('aes') and item.get('xor') is not None:
        xor = item['xor']
        if not isinstance(xor, int):
            value = str(xor).strip()
            # WeFlow stores decimal bytes; this application's store uses hex.
            xor = int(value, 16 if item.get('xor_format') == 'hex' or value.lower().startswith('0x') or not value.isdecimal() else 10)
        aes = str(item['aes'])[:16]
        cached = get_account_keys_from_store(account)
        if (cached.get('image_key_verified') and cached.get('image_aes_key') == aes
                and cached.get('image_xor_key') == f'{xor:02x}'
                and cached.get('image_key_source_wxid_dir') == str(source.parent)):
            return {'account': account, 'changed': changed, 'unchanged': len(skipped),
                    'failed': failures, 'image_key_verified': True}
        templates = scan_v2_templates(source.parent)
        if verify_key_pair(xor, aes, templates):
            upsert_account_keys_in_store(account, image_xor_key=f'{xor:02x}', image_aes_key=aes,
                image_key_verified=True, image_key_source='weflow_linux_verified',
                image_key_source_wxid_dir=str(source.parent))
            image_verified = True
    return {'account': account, 'changed': changed, 'unchanged': len(skipped),
            'failed': failures, 'image_key_verified': image_verified}

def sync_weflow(config_path: str | Path, *, force: bool = False) -> list[dict]:
    return [sync_account(item, force=force) for item in available_accounts(config_path)]
