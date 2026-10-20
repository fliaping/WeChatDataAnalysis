import asyncio
import contextlib
import json
import shutil
import sqlite3
import time

from wechat_decrypt_tool import linux_support as linux
from wechat_decrypt_tool import linux_snapshot_watch as watch


def test_real_filesystem_event_publishes_snapshot_within_three_seconds(tmp_path, monkeypatch):
    source = tmp_path / 'local/db_storage'
    source.mkdir(parents=True)
    original = source / 'session.db'
    with sqlite3.connect(original) as db:
        db.execute('CREATE TABLE fixture(value TEXT)')
    output = tmp_path / 'output'
    monkeypatch.setattr(linux, 'get_output_databases_dir', lambda: output)
    monkeypatch.setattr(linux, 'upsert_account_keys_in_store', lambda *a, **kw: None)
    item = {'account': 'local', 'source': source, 'key': 'fixture'}
    monkeypatch.setattr(linux, 'available_accounts', lambda _: [item])
    monkeypatch.setattr(watch, 'local_sources', lambda _: {'local': source})

    class PlainFixture:
        def __init__(self, key):
            pass

        def decrypt_database(self, source, target):
            with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
                src.backup(dst)
            return True

    monkeypatch.setattr(linux, 'WeChatDatabaseDecryptor', PlainFixture)

    async def run():
        monitor = watch.SnapshotWatch(tmp_path / 'config.json')
        task = asyncio.create_task(monitor.run())
        try:
            async with asyncio.timeout(6):
                while monitor.state['mode'] != 'filesystem_events':
                    await asyncio.sleep(.02)
                await asyncio.sleep(.2)
                report = output / 'local/_linux_sync.json'
                baseline = json.loads(report.read_text())['revision']
                with sqlite3.connect(original) as db:
                    db.execute("INSERT INTO fixture VALUES ('synthetic change')")
                started = time.monotonic()
                while json.loads(report.read_text())['revision'] == baseline:
                    await asyncio.sleep(.02)
                assert time.monotonic() - started < 3
                with sqlite3.connect(output / 'local/session.db') as db:
                    assert db.execute('SELECT value FROM fixture').fetchone() == ('synthetic change',)
                # Rename/replacement produces a second distinct publication.
                replacement = source / 'replacement'
                shutil.copyfile(original, replacement)
                with sqlite3.connect(replacement) as db:
                    db.execute("INSERT INTO fixture VALUES ('replacement')")
                replacement.replace(original)
                while json.loads(report.read_text())['revision'] < baseline + 2:
                    await asyncio.sleep(.02)
                with sqlite3.connect(output / 'local/session.db') as db:
                    assert db.execute('SELECT count(*) FROM fixture').fetchone()[0] == 2
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    asyncio.run(run())


def test_verified_image_keys_skip_repeated_media_scans(tmp_path, monkeypatch):
    source = tmp_path / 'local/db_storage'
    source.mkdir(parents=True)
    monkeypatch.setattr(linux, 'get_output_databases_dir', lambda: tmp_path / 'output')
    monkeypatch.setattr(linux, 'get_account_keys_from_store', lambda _: {
        'image_key_verified': True, 'image_aes_key': '0123456789abcdef',
        'image_xor_key': 'a6', 'image_key_source_wxid_dir': str(source.parent)})
    def unexpected_scan(*args):
        raise AssertionError('Already verified media must not be scanned every two seconds')
    monkeypatch.setattr(linux, 'scan_v2_templates', unexpected_scan)
    result = linux.sync_account({'account': 'local', 'source': source, 'key': 'c' * 64,
                                'aes': '0123456789abcdef', 'xor': 166})
    assert result['image_key_verified']


def test_only_database_and_wal_events_trigger_publication():
    assert watch.database_change(None, '/data/session.db-wal')
    assert watch.database_change(None, '/data/session.db')
    assert not watch.database_change(None, '/data/session.db-shm')
    assert not watch.database_change(None, '/data/image.dat')
