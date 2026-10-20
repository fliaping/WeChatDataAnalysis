import asyncio
import json
import shutil
import sqlite3
from unittest.mock import patch

from wechat_decrypt_tool import linux_support as linux
from wechat_decrypt_tool.linux_snapshot_events import stream_linux_snapshot_events
from wechat_decrypt_tool.routers import chat, chat_realtime_sse


def make_account(tmp_path, mode='linux_snapshot'):
    account = tmp_path / 'account'
    account.mkdir()
    for name in ('session.db', 'contact.db'):
        with sqlite3.connect(account / name) as db:
            db.execute('CREATE TABLE fixture(value TEXT)')
    (account / '_source.json').write_text(json.dumps({'import_mode': mode}))
    return account


def test_linux_status_and_both_streams_never_open_native_wcdb(tmp_path):
    account = make_account(tmp_path)

    class Request:
        headers = {}

    async def run():
        status = await chat.get_chat_realtime_status('account')
        assert status['available']
        assert status['realtime']['mode'] == 'linux_snapshot'
        assert status['realtime']['snapshot_preferred']
        assert not status['realtime']['connected']
        for endpoint in (chat.stream_chat_realtime_events, chat_realtime_sse.stream_chat_realtime_messages):
            response = await endpoint(Request(), account='account')
            assert response.media_type == 'text/event-stream'
        (account / '_source.json').write_text(json.dumps({'import_mode': 'manual_import'}))
        assert not (await chat.get_chat_realtime_status('account'))['available']

    with (patch.object(chat, '_resolve_account_dir', return_value=account),
          patch.object(chat_realtime_sse, '_resolve_account_dir', return_value=account),
          patch.object(chat.WCDB_REALTIME, 'ensure_connected', side_effect=AssertionError('native connection')),
          patch.object(chat.WCDB_REALTIME, 'get_status', side_effect=AssertionError('native status'))):
        asyncio.run(run())


def test_stream_notifies_on_published_revision_and_reconnect(tmp_path):
    account = make_account(tmp_path)
    report = account / '_linux_sync.json'
    report.write_text(json.dumps({'revision': 1}))

    class Request:
        async def is_disconnected(self):
            return False

    async def run():
        response = stream_linux_snapshot_events(Request(), account)
        stream = response.body_iterator
        ready = await anext(stream)
        assert '"type":"ready"' in ready and '"revision":1' in ready
        pending = asyncio.create_task(anext(stream))
        try:
            # Unchanged sync attempts must not refresh the chat or reset scroll.
            report.write_text(json.dumps({'revision': 1, 'last_attempt_at': 123}))
            await asyncio.sleep(1.1)
            assert not pending.done()
            report.write_text(json.dumps({'revision': 2}))
            event = await asyncio.wait_for(pending, timeout=2)
            assert '"type":"snapshot_updated"' in event and '"revision":2' in event
        finally:
            if not pending.done():
                pending.cancel()
            await stream.aclose()

    asyncio.run(run())


def test_sync_revision_tracks_validated_publications_and_preserves_failed_database(tmp_path, monkeypatch):
    source = tmp_path / 'source'
    source.mkdir()
    original = source / 'session.db'
    with sqlite3.connect(original) as db:
        db.execute('CREATE TABLE fixture(value TEXT)')
    monkeypatch.setattr(linux, 'get_output_databases_dir', lambda: tmp_path / 'output')
    monkeypatch.setattr(linux, 'upsert_account_keys_in_store', lambda *a, **kw: None)

    class Decryptor:
        def __init__(self, key):
            pass

        def decrypt_database(self, source, target):
            shutil.copyfile(source, target)
            return True

    monkeypatch.setattr(linux, 'WeChatDatabaseDecryptor', Decryptor)
    item = {'account': 'fixture', 'source': source, 'key': 'test-key'}
    output = tmp_path / 'output/fixture'
    report = output / '_linux_sync.json'
    assert linux.sync_account(item)['changed'] == ['session.db']
    assert json.loads(report.read_text())['revision'] == 1
    assert linux.sync_account(item)['unchanged'] == 1
    assert json.loads(report.read_text())['revision'] == 1
    with sqlite3.connect(original) as db:
        db.execute("INSERT INTO fixture VALUES ('synthetic new message')")
    assert linux.sync_account(item)['changed'] == ['session.db']
    assert json.loads(report.read_text())['revision'] == 2
    with sqlite3.connect(output / 'session.db') as db:
        assert db.execute('SELECT count(*) FROM fixture').fetchone()[0] == 1
    previous = (output / 'session.db').read_bytes()
    original.write_bytes(b'invalid database')
    assert len(linux.sync_account(item)['failed']) == 1
    assert (output / 'session.db').read_bytes() == previous
    assert json.loads(report.read_text())['revision'] == 2
    assert json.loads(report.read_text())['failed_count'] == 1
