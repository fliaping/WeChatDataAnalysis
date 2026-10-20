"""Exercise decrypted-copy transactions and remote submission boundaries."""
import asyncio
import hashlib
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI
from wechat_decrypt_tool.advanced import contacts, editing, service, store, tasks, moments
from wechat_decrypt_tool.advanced.weharbor import WeHarbor, BridgeError
from wechat_decrypt_tool.routers import advanced


class IsolatedState(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.environment = patch.dict(os.environ, {'WECHAT_TOOL_DATA_DIR': str(self.root),
            'WECHAT_TOOL_OUTPUT_DIR': str(self.root / 'output'), 'WEHARBOR_API_TOKEN': '',
            'WEHARBOR_TOKEN_FILE': str(self.root / 'missing'), 'WEHARBOR_URL': 'http://127.0.0.1:8766'})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.addCleanup(self.temp.cleanup)


class ContactSnapshotTests(IsolatedState):
    def test_complete_snapshots_report_changes_without_deletion_claims(self):
        baseline = [{'username': 'a', 'nickname': 'Alice'}, {'username': 'b', 'nickname': 'Bob'}]
        self.assertEqual(contacts.record('self', baseline, 2)['changes'], [])
        result = contacts.record('self', [{'username': 'a', 'nickname': 'New name'}, {'username': 'c'}], 2)
        changes = {row['username']: row for row in result['changes']}
        self.assertEqual(changes['a']['kind'], 'changed')
        self.assertEqual(changes['a']['before']['nickname'], 'Alice')
        self.assertEqual(changes['b']['kind'], 'not_in_current_list')
        self.assertEqual(changes['c']['kind'], 'added')
        self.assertEqual(len(contacts.record('self', [{'username': 'a', 'nickname': 'New name'}, {'username': 'c'}], 2)['changes']), 3)
        self.assertEqual(contacts.history('other')['changes'], [])

    def test_incomplete_or_duplicate_snapshot_never_replaces_baseline(self):
        contacts.record('self', [{'username': 'a'}], 1)
        for items, total in [([], 1), ([{'username': 'a'}, {'username': 'a'}], 2), ([{}], 1)]:
            with self.assertRaises(ValueError):
                contacts.record('self', items, total)
        self.assertEqual(contacts.record('self', [{'username': 'a'}], 1)['changes'], [])


class EditingTests(IsolatedState):
    def setUp(self):
        super().setUp()
        self.account = self.root / 'output/databases/wxid_self'
        self.account.mkdir(parents=True)
        for name in ('session.db', 'contact.db'):
            with sqlite3.connect(self.account / name) as db:
                db.execute('CREATE TABLE fixture(id INTEGER)')
        self.table = 'Msg_' + hashlib.md5(b'wxid_friend').hexdigest()
        self.path = self.account / 'message_0.db'
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE Name2Id(user_name TEXT)')
            db.executemany('INSERT INTO Name2Id VALUES (?)', [('wxid_self',), ('wxid_friend',)])
            db.execute('CREATE TABLE ' + self.table + '''(local_id INTEGER PRIMARY KEY, server_id INTEGER,
                local_type INTEGER, sort_seq INTEGER, real_sender_id INTEGER, create_time INTEGER,
                message_content TEXT, compress_content BLOB, source TEXT, WCDB_CT_message_content INTEGER,
                WCDB_CT_source INTEGER)''')
            db.executemany('INSERT INTO ' + self.table + ' VALUES (?,?,?,?,?,?,?,?,?,?,?)', [
                (1, 123, 1, 1000000, 1, 1000, 'before', b'compressed', 'old', 4, 4),
                (2, 124, 10000, 1001000, 2, 1001, 'system', None, '', None, None)])
        self.base = {'account': 'wxid_self', 'username': 'wxid_friend',
                     'message_id': 'message_0:' + self.table + ':1'}

    def rows(self):
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute('SELECT * FROM ' + self.table + ' ORDER BY local_id')]

    def apply(self, feature, **kwargs):
        value = self.base | {'feature': feature} | kwargs
        value['revision'] = editing.preview(value)['revision']
        return editing.apply(value)

    def test_text_edit_and_undo_preserve_original_blob(self):
        before = self.rows()
        receipt = self.apply('edit-text', text='after')
        self.assertEqual(self.rows()[0]['message_content'], 'after')
        self.assertIsNone(self.rows()[0]['compress_content'])
        self.assertIsNone(self.rows()[0]['WCDB_CT_message_content'])
        editing.restore('wxid_self', receipt['journal_id'])
        self.assertEqual(self.rows(), before)
        with self.assertRaises(ValueError):
            editing.restore('wxid_self', receipt['journal_id'])

    def test_stale_preview_is_rejected(self):
        value = self.base | {'feature': 'edit-text', 'text': 'stale'}
        value['revision'] = editing.preview(value)['revision']
        self.apply('edit-text', text='newer')
        with self.assertRaisesRegex(ValueError, '变化'):
            editing.apply(value)
        self.assertEqual(self.rows()[0]['message_content'], 'newer')

    def test_undo_will_not_overwrite_sync(self):
        receipt = self.apply('edit-text', text='after')
        with sqlite3.connect(self.path) as db:
            db.execute('UPDATE ' + self.table + ' SET message_content=? WHERE local_id=1', ('synced',))
        with self.assertRaisesRegex(ValueError, '同步'):
            editing.restore('wxid_self', receipt['journal_id'])
        self.assertEqual(self.rows()[0]['message_content'], 'synced')

    def test_all_add_types_have_real_persistent_rows_and_undo(self):
        before = self.rows()
        for kind, local_type in editing.TYPES.items():
            with self.subTest(kind=kind):
                value = self.base | {'feature': 'add-' + kind, 'text': 'fixture',
                    'xml': '<msg><appmsg><title>fixture</title></appmsg></msg>', 'create_time': 1002}
                receipt = editing.apply(value)
                self.assertEqual(self.rows()[-1]['local_type'], local_type)
                editing.restore('wxid_self', receipt['journal_id'])
                self.assertEqual(self.rows(), before)

    def test_complex_add_requires_xml_and_bad_xml_rolls_back(self):
        before = self.rows()
        for extra in ({}, {'xml': '<bad'}):
            with self.assertRaises(ValueError):
                editing.apply(self.base | {'feature': 'add-image'} | extra)
            self.assertEqual(self.rows(), before)

    def test_system_delete_and_restore(self):
        value = self.base | {'feature': 'edit-delete-sys'}
        value.pop('message_id')
        value['revision'] = editing.preview(value)['revision']
        before = self.rows()
        receipt = editing.apply(value)
        self.assertEqual([r['local_id'] for r in self.rows()], [1])
        editing.restore('wxid_self', receipt['journal_id'])
        self.assertEqual(self.rows(), before)

    def test_source_time_and_field_edits(self):
        for feature, values, field, expected in [
            ('edit-source', {'xml': '<msg><appmsg><title>fixed</title></appmsg></msg>'}, 'message_content', '<msg><appmsg><title>fixed</title></appmsg></msg>'),
            ('edit-time', {'create_time': 2000}, 'sort_seq', 2000000),
            ('edit-fields', {'fields': {'server_id': 999}}, 'server_id', 999)]:
            before = self.rows()
            receipt = self.apply(feature, **values)
            self.assertEqual(self.rows()[0][field], expected)
            editing.restore('wxid_self', receipt['journal_id'])
            self.assertEqual(self.rows(), before)

    def test_sender_mapping_rolls_back_all_rows_on_error(self):
        value = self.base | {'feature': 'edit-fix-sender', 'mapping': {'1': 2, '2': 999}}
        value.pop('message_id')
        value['revision'] = editing.preview(value)['revision']
        before = self.rows()
        with self.assertRaises(ValueError):
            editing.apply(value)
        self.assertEqual(self.rows(), before)
        self.assertEqual(editing.history('wxid_self'), [])

    def test_local_id_change_and_undo_preserve_identity_and_collisions(self):
        before = self.rows()
        receipt = self.apply('edit-fields', fields={'local_id': 9})
        self.assertEqual(self.rows()[-1]['local_id'], 9)
        editing.restore('wxid_self', receipt['journal_id'])
        self.assertEqual(self.rows(), before)
        with self.assertRaises(sqlite3.IntegrityError):
            self.apply('edit-fields', fields={'local_id': 2})
        self.assertEqual(self.rows(), before)

    def test_flip_uses_explicit_account_identity(self):
        (self.account / 'account.json').write_text(json.dumps({'username': 'wxid_self'}))
        receipt = self.apply('edit-flip-sides')
        self.assertEqual(self.rows()[0]['real_sender_id'], 2)
        editing.restore('wxid_self', receipt['journal_id'])
        self.assertEqual(self.rows()[0]['real_sender_id'], 1)

    def test_message_id_cannot_cross_conversations_or_database_paths(self):
        for identifier in ('../message_0:' + self.table + ':1', 'message_0:Msg_other:1'):
            with self.assertRaises(ValueError):
                editing.preview(self.base | {'message_id': identifier})
        other = self.account / 'message_1.db'
        other.symlink_to(self.path)
        with self.assertRaises(ValueError):
            editing.preview(self.base | {'message_id': 'message_1:' + self.table + ':1'})


class RemoteTests(IsolatedState, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        service.ACTION_LOCK = asyncio.Lock()
        tasks.LOCK = asyncio.Lock()
        self.remote = AsyncMock()
        self.remote.settings = store.config()
        self.remote.tools.return_value = [{'name': n} for n in ('wechat_prepare_send', 'wechat_commit_send')]
        self.remote.request.side_effect = self.respond
        self.calls = []
        def bridge():
            self.remote.settings = store.config()
            return self.remote
        self.mock = patch.object(service, 'WeHarbor', side_effect=bridge)
        self.mock.start()
        self.addCleanup(self.mock.stop)
        self.payload = {'account': 'self', 'username': 'friend', 'text': 'exact text'}

    async def respond(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        return {'id': 'remote-1', 'status': 'prepared' if path.endswith('/prepare') else 'sent'}

    async def test_prepare_is_idempotent_and_commit_submits_once(self):
        first = await service.prepare('send-text', self.payload, 'key')
        again = await service.prepare('send-text', self.payload, 'key')
        self.assertEqual(first, again)
        self.assertEqual(len(self.calls), 1)
        with self.assertRaises(ValueError):
            await service.prepare('send-text', self.payload | {'text': 'different'}, 'key')
        results = await asyncio.gather(service.commit('key'), service.commit('key'))
        self.assertEqual([r['status'] for r in results], ['sent', 'sent'])
        self.assertEqual(len(self.calls), 2)

    async def test_interrupted_submit_is_never_retried(self):
        await service.prepare('send-text', self.payload, 'key')
        self.remote.request.side_effect = BridgeError('disconnected')
        with self.assertRaises(BridgeError):
            await service.commit('key')
        self.assertEqual(service.receipt('key')['status'], 'uncertain')
        self.remote.request.reset_mock()
        self.assertEqual((await service.commit('key'))['status'], 'uncertain')
        self.remote.request.assert_not_awaited()

    async def test_reconfiguration_prevents_redirecting_prepared_action(self):
        await service.prepare('send-text', self.payload, 'key')
        with patch.dict(os.environ, {'WEHARBOR_URL': 'http://other:8766'}):
            result = await service.commit('key')
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(len(self.calls), 1)

    async def test_missing_driver_is_blocked_without_fake_success(self):
        caps = await service.capabilities()
        self.assertEqual(caps['counts']['total'], 79)
        self.assertEqual(caps['counts']['available'], 31)
        self.assertEqual(caps['features']['send-text']['state'], 'available')
        self.assertEqual(caps['features']['send-image']['state'], 'blocked')
        self.assertEqual(caps['features']['contact-search']['state'], 'blocked')
        self.assertEqual(caps['features']['send-image']['implementation'], 'implemented')
        self.assertEqual(caps['features']['contact-search']['implementation'], 'pending')
        self.assertTrue(all(c['unlocked'] for c in caps['features'].values()))
        with self.assertRaisesRegex(ValueError, '尚未实现'):
            await service.prepare('send-image', self.payload, 'key')
        self.assertEqual(self.calls, [])

    async def test_expired_preparation_cannot_submit(self):
        await service.prepare('send-text', self.payload, 'key')
        with store.connect() as db:
            db.execute('UPDATE receipts SET created=? WHERE id=?', (time.time() - 121, 'key'))
        self.assertEqual((await service.commit('key'))['status'], 'expired')
        self.assertEqual(len(self.calls), 1)

    async def test_native_tools_enable_nine_features_without_claiming_other_drivers(self):
        self.remote.tools.return_value += [{'name': 'wechat_' + feature.replace('-', '_')}
                                           for feature in service.DESKTOP_FEATURES if 'label' not in feature]
        self.remote.tools.return_value += [{'name': 'wechat_contact_lookup'}]
        caps = await service.capabilities()
        self.assertEqual(caps['counts'], {'total': 79, 'available': 40})
        for feature in ('contact-search', 'sns-post', 'automation-onboarding'):
            self.assertEqual(caps['features'][feature]['state'], 'blocked')

    async def test_label_tools_unlock_labels_and_onboarding_only_when_published(self):
        self.remote.tools.return_value += [{'name': 'wechat_' + feature.replace('-', '_'), 'annotations': {'readOnlyHint': feature == 'contact-list-labels'}}
                                           for feature in service.DESKTOP_FEATURES | {'contact-list-labels'}]
        caps = await service.capabilities()
        self.assertEqual(caps['counts'], {'total': 79, 'available': 46})
        self.assertEqual(caps['features']['automation-onboarding']['state'], 'available')
        self.assertEqual(caps['features']['automation-moments-sync']['state'], 'blocked')
        self.assertNotIn('automation-moments-follow', caps['features'])

    async def test_moments_driver_unlocks_refresh_and_continuous_archive(self):
        self.remote.tools.return_value += [{'name': 'wechat_' + feature.replace('-', '_'), 'annotations': {'readOnlyHint': feature == 'contact-list-labels'}}
                                           for feature in service.DESKTOP_FEATURES | {'contact-list-labels', 'sns-autorefresh'}]
        caps = await service.capabilities()
        self.assertEqual(caps['counts'], {'total': 79, 'available': 48})
        self.remote.call.return_value = {'status': 'completed', 'desktop': {'refresh_requested': True}}
        result = await service.refresh_moments('self')
        self.assertEqual(result['status'], 'completed')
        self.remote.call.assert_awaited_once_with('wechat_sns_autorefresh', {'account': 'self'})
        self.remote.call.return_value = {'status': 'uncertain'}
        with self.assertRaises(ValueError):
            await service.refresh_moments('self')

    async def test_moments_task_is_inactive_and_rejects_interactions_and_bad_intervals(self):
        plan = {'kind': 'automation-moments-sync', 'account': 'self', 'interval': 30}
        with self.assertRaises(ValueError):
            await tasks.create(plan)
        self.remote.tools.return_value += [{'name': 'wechat_sns_autorefresh'}]
        for invalid in [plan | {'steps': [{'feature': 'sns-like', 'payload': {}}]},
                        plan | {'interval': 29}, plan | {'interval': float('nan')},
                        plan | {'due': float('inf')}, plan | {'format': 'exe'}]:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                await tasks.create(invalid)
        task = await tasks.create(plan)
        self.assertEqual(task['status'], 'draft')
        with patch.object(moments, 'tick', new=AsyncMock()) as tick, patch.object(tasks, 'source', new=AsyncMock()) as source:
            await tasks.tick(None, task['id'])
            tick.assert_not_awaited()
            await tasks.operate(task['id'], 'activate', None)
            source.assert_not_awaited()
            await tasks.tick(None, task['id'])
            tick.assert_awaited_once()
        self.assertEqual(self.calls, [])

    async def test_moments_archive_reuses_folder_after_restart_and_waits_for_exports(self):
        self.remote.tools.return_value += [{'name': 'wechat_sns_autorefresh'}]
        task = await tasks.create({'kind': 'automation-moments-sync', 'account': 'self', 'targets': ['friend'], 'interval': 60})
        await tasks.operate(task['id'], 'activate', None)
        exports = SimpleNamespace(get_job=lambda identifier: None, create_job=lambda **kw: SimpleNamespace(export_id='export-one'))
        with patch.object(moments, 'manager', return_value=exports), patch.object(service, 'refresh_moments', new=AsyncMock(return_value={'status': 'completed'})) as refresh:
            await tasks.tick(None, task['id'])
            task = tasks.get(task['id'])
            folder = task['value']['output_dir']
            self.assertEqual(Path(folder).stat().st_mode & 0o777, 0o700)
            task['due'] = 0; tasks.save(task)
            # Simulate restart: old export ID has no corresponding in-memory job.
            await tasks.tick(None, task['id'])
            self.assertEqual(tasks.get(task['id'])['value']['output_dir'], folder)
            self.assertEqual(refresh.await_count, 2)
            job = SimpleNamespace(export_id='export-one', status='running')
            exports.get_job = lambda identifier: job
            task = tasks.get(task['id']); task['due'] = 0; tasks.save(task)
            await tasks.tick(None, task['id'])
            self.assertEqual(refresh.await_count, 2)
            job.status, job.folder_path, job.warning = 'done', Path(folder) / 'archive', 'some media unavailable'
            job.to_public_dict = lambda: {'status': 'done', 'incremental': {'added': 1}}
            released = []
            exports.release_job = released.append
            task = tasks.get(task['id']); task['due'] = 0; tasks.save(task)
            await tasks.tick(None, task['id'])
            task = tasks.get(task['id'])
            self.assertEqual(task['cursor'], 1)
            self.assertNotIn('export_id', task['value'])
            self.assertIn('some media unavailable', task['result'])
            self.assertEqual(released, ['export-one'])
            self.assertEqual(task['value']['last_export']['incremental'], {'added': 1})

    async def test_pausing_moments_cancels_running_export_and_legacy_tasks_never_run(self):
        self.remote.tools.return_value += [{'name': 'wechat_sns_autorefresh'}]
        task = await tasks.create({'kind': 'automation-moments-sync', 'account': 'self'})
        await tasks.operate(task['id'], 'activate', None)
        task = tasks.get(task['id']); task['value']['export_id'] = 'running'; tasks.save(task)
        with patch.object(moments, 'cancel_export') as cancel:
            await tasks.operate(task['id'], 'pause', None)
            self.assertEqual(cancel.call_args.args[0]['export_id'], 'running')
        task = tasks.get(task['id'])
        task['value']['kind'] = 'automation-moments-follow'; task['status'] = 'active'; task['due'] = time.time() + 3600; tasks.save(task)
        with patch.object(service, 'prepare', new=AsyncMock()) as prepare:
            await tasks.tick(None, task['id'])
            self.assertEqual(tasks.get(task['id'])['status'], 'paused')
            with self.assertRaisesRegex(ValueError, '已停用'):
                await tasks.operate(task['id'], 'resume', None)
            prepare.assert_not_awaited()

    async def test_read_labels_requires_a_read_only_tool_and_never_prepares_a_write(self):
        self.remote.tools.return_value = [{'name': 'wechat_contact_list_labels', 'annotations': {'readOnlyHint': True}}]
        await service.read_labels('self', 'friend')
        self.remote.call.assert_awaited_once_with('wechat_contact_list_labels', {'account': 'self', 'username': 'friend'})
        self.assertEqual(self.calls, [])
        with self.assertRaises(ValueError):
            await service.prepare('contact-list-labels', {'account': 'self', 'username': 'friend'}, 'read')
        self.remote.tools.return_value[0]['annotations']['readOnlyHint'] = False
        with self.assertRaises(ValueError):
            await service.read_labels('self', 'friend')

    def desktop_tools(self, generic=False):
        tools = [{'name': 'wechat_chat_set_mute', 'inputSchema': {'type': 'object', 'properties': {
            'account': {'type': 'string'}, 'username': {'type': 'string'},
            'muted': {'type': 'boolean'}, 'idempotency_key': {'type': 'string'}},
            'required': ['account', 'username', 'muted', 'idempotency_key'], 'additionalProperties': False}}]
        if not generic:
            tools += [{'name': n} for n in ['wechat_prepare_operation', 'wechat_commit_operation', 'wechat_cancel_operation']]
        self.remote.tools.return_value = tools
        return tools

    async def test_desktop_prepare_captures_exact_arguments_without_mutation(self):
        self.desktop_tools()
        value = {'account': 'self', 'username': 'friend', 'parameters': {'muted': False}}
        item = await service.prepare('chat-set-mute', value, 'key')
        self.assertEqual(item['status'], 'prepared')
        self.assertEqual(self.calls, [('POST', '/operations/prepare', {'json': {'operation': 'chat-set-mute', 'account': 'self', 'username': 'friend', 'parameters': {'muted': False}, 'idempotency_key': 'key'}})])
        self.remote.call.assert_not_awaited()
        with self.assertRaisesRegex(ValueError, '幂等键'):
            await service.prepare('chat-set-mute', value | {'parameters': {'muted': False, 'idempotency_key': 'other'}}, 'another')

    async def test_desktop_uncertain_receipt_is_never_promoted_or_retried(self):
        self.desktop_tools()
        await service.prepare('chat-set-mute', {'account': 'self', 'username': 'friend', 'parameters': {'muted': True}}, 'key')
        self.remote.request.side_effect = None
        self.remote.request.return_value = {'id': 'remote-1', 'status': 'uncertain'}
        self.assertEqual((await service.commit('key'))['status'], 'uncertain')
        self.remote.request.assert_awaited_with('POST', '/operations/remote-1/commit')
        self.remote.request.reset_mock()
        await service.commit('key')
        self.remote.request.assert_not_awaited()

    async def test_recovered_remote_terminal_receipt_is_not_prepared_again(self):
        self.desktop_tools()
        self.remote.request.side_effect = None
        self.remote.request.return_value = {'id': 'remote-1', 'status': 'completed'}
        item = await service.prepare('chat-set-mute', {'account': 'self', 'username': 'friend', 'parameters': {'muted': True}}, 'key')
        self.assertEqual(item['status'], 'completed')
        self.remote.request.reset_mock()
        self.assertEqual((await service.commit('key'))['status'], 'completed')
        self.remote.request.assert_not_awaited()

    async def test_custom_binding_is_not_redirected_to_native_operation(self):
        tools = self.desktop_tools()
        self.remote.tools.return_value = [tools[0] | {'name': 'custom_mute'}, *tools[1:]]
        with patch.object(service, 'tool_name', return_value='custom_mute'):
            item = await service.prepare('chat-set-mute', {'account': 'self', 'username': 'friend', 'parameters': {'muted': False}}, 'key')
            self.assertEqual(item['result']['mode'], 'custom_mute')
            self.assertEqual(self.calls, [])
            self.remote.call.return_value = {'status': 'completed'}
            await service.commit('key')
        self.remote.call.assert_awaited_once_with('custom_mute', {'account': 'self', 'username': 'friend', 'muted': False, 'idempotency_key': 'key'})

    async def test_changed_desktop_schema_cancels_remote_preparation(self):
        tools = self.desktop_tools()
        await service.prepare('chat-set-mute', {'account': 'self', 'username': 'friend', 'parameters': {'muted': True}}, 'key')
        self.remote.tools.return_value = [tools[0] | {'inputSchema': {}}, *tools[1:]]
        self.remote.request.side_effect = None
        self.remote.request.return_value = {'id': 'remote-1', 'status': 'cancelled'}
        self.assertEqual((await service.commit('key'))['status'], 'cancelled')
        self.remote.request.assert_awaited_with('POST', '/operations/remote-1/cancel')

    async def test_desktop_cancel_uses_operations_not_text_actions(self):
        self.desktop_tools()
        await service.prepare('chat-set-mute', {'account': 'self', 'username': 'friend', 'parameters': {'muted': False}}, 'key')
        self.remote.request.side_effect = None
        self.remote.request.return_value = {'id': 'remote-1', 'status': 'cancelled'}
        self.assertEqual((await service.cancel('key'))['status'], 'cancelled')
        self.remote.request.assert_awaited_with('POST', '/operations/remote-1/cancel')

    async def test_generic_tools_use_saved_key_and_propagate_mcp_status(self):
        self.desktop_tools(generic=True)
        await service.prepare('chat-set-mute', {'account': 'self', 'username': 'friend', 'parameters': {'muted': False}}, 'key')
        self.remote.call.return_value = {'content': [{'type': 'text', 'text': json.dumps({'status': 'uncertain'})}]}
        self.assertEqual((await service.commit('key'))['status'], 'uncertain')
        self.remote.call.assert_awaited_once_with('wechat_chat_set_mute', {'account': 'self', 'username': 'friend', 'muted': False, 'idempotency_key': 'key'})

    def test_malformed_remote_states_never_become_completed(self):
        for status in [None, [], {}, 'submitted', 'unknown']:
            self.assertEqual(service.remote_status({'status': status}), 'uncertain')
        self.assertEqual(service.remote_status({'content': [{'type': 'text', 'text': '{"status":"failed"}'}]}), 'failed')

    async def test_task_draft_activation_and_restart_use_same_receipt(self):
        task = await tasks.create({'kind': 'automation-broadcast', 'account': 'self', 'targets': ['friend'],
            'steps': [{'feature': 'send-text', 'payload': {'text': 'exact text'}}], 'interval': 5})
        await tasks.tick(None, task['id'])
        self.assertEqual(self.calls, [])
        await tasks.operate(task['id'], 'activate', None)
        # Simulate receipt committed just before a process dies before saving cursor.
        task = tasks.get(task['id'])
        step = task['value']['queue'][0]
        key = 'task-' + task['id'] + '-0'
        await service.prepare(step['feature'], step['payload'], key)
        await service.commit(key)
        await tasks.tick(None, task['id'])
        self.assertEqual(tasks.get(task['id'])['cursor'], 1)
        self.assertEqual(len(self.calls), 2)

    async def test_image_broadcast_binds_each_step_to_its_authorized_target(self):
        self.remote.tools.return_value += [{'name': 'wechat_send_image'}]
        task = await tasks.create({'kind': 'automation-broadcast', 'account': 'self', 'targets': ['first', 'second'],
            'steps': [{'feature': 'send-image', 'payload': {'username': 'other',
                'parameters': {'path': '/private/upload.png', 'username': 'other'}}}], 'interval': 5})
        await tasks.operate(task['id'], 'activate', None)
        queue = tasks.get(task['id'])['value']['queue']
        self.assertEqual([step['payload']['username'] for step in queue], ['first', 'second'])
        self.assertEqual([step['payload']['parameters']['username'] for step in queue], ['first', 'second'])
        self.assertEqual([step['payload']['account'] for step in queue], ['self', 'self'])
        self.assertEqual(self.calls, [])


    async def test_onboarding_baseline_and_filters_never_replay_existing_friends(self):
        self.remote.tools.return_value += [{'name': 'wechat_' + feature.replace('-', '_')} for feature in service.DESKTOP_FEATURES]
        task = await tasks.create({'kind': 'automation-onboarding', 'account': 'self', 'targets': ['new'],
            'steps': [{'feature': 'chat-mark-read', 'payload': {'parameters': {}}},
                      {'feature': 'contact-set-labels', 'payload': {'parameters': {'labels': ['Friends']}}}], 'interval': 5})
        baseline = [{'username': 'old', 'type': 'friend'}]
        later = baseline + [{'username': 'new', 'type': 'friend'}, {'username': 'other', 'type': 'friend'}]
        with patch.object(tasks, 'source', new=AsyncMock(side_effect=[baseline, later, later])), \
             patch.object(service, 'prepare', new=AsyncMock(return_value={'id': 'prepared'})) as prepare, \
             patch.object(service, 'commit', new=AsyncMock(return_value={'status': 'completed'})):
            await tasks.operate(task['id'], 'activate', None)
            self.assertEqual(tasks.get(task['id'])['value']['queue'], [])
            await tasks.tick(None, task['id'])
            pending = tasks.get(task['id'])
            self.assertEqual([step['feature'] for step in pending['value']['queue']], ['chat-mark-read', 'contact-set-labels'])
            self.assertEqual([step['payload']['username'] for step in pending['value']['queue']], ['new', 'new'])
            pending['due'] = 0
            tasks.save(pending)
            await tasks.tick(None, task['id'])
            pending = tasks.get(task['id'])
            pending['due'] = 0
            tasks.save(pending)
            await tasks.tick(None, task['id'])
            self.assertEqual(prepare.await_count, 2)
            self.assertEqual(tasks.get(task['id'])['cursor'], 2)

    async def test_onboarding_source_rejects_partial_wrong_account_or_duplicate_snapshots(self):
        plan = {'kind': 'automation-onboarding', 'account': 'self'}
        rows = [{'username': 'old', 'type': 'friend'}]
        for result in [{'contacts': rows, 'total': 2}, {'contacts': rows, 'total': 1, 'account': 'other'},
                       {'contacts': rows * 2, 'total': 2}]:
            with self.subTest(result=result), patch.object(advanced, 'local_read', new=AsyncMock(return_value=result)), self.assertRaises(ValueError):
                await tasks.source(None, plan)

    async def test_native_parameter_target_must_match_the_reviewed_payload(self):
        self.desktop_tools()
        with self.assertRaises(ValueError):
            await service.prepare('chat-set-mute', {'account': 'self', 'username': 'friend',
                'parameters': {'username': 'someone-else', 'muted': True}}, 'mismatch')
        self.assertEqual(self.calls, [])


class ApiTests(IsolatedState, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = FastAPI()
        self.app.include_router(advanced.router)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://test')
        self.addAsyncCleanup(self.client.aclose)

    async def test_settings_never_expose_environment_token_or_copy_it_to_disk(self):
        with patch.dict(os.environ, {'WEHARBOR_API_TOKEN': 'environment-secret'}):
            response = await self.client.put('/api/advanced/settings', json={'harness_url': '/assistant/'})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('environment-secret', response.text)
        self.assertTrue(response.json()['token_configured'])
        with store.connect() as db:
            self.assertNotIn('environment-secret', db.execute('SELECT value FROM config').fetchone()[0])

    async def test_cross_site_change_rejected(self):
        response = await self.client.put('/api/advanced/settings', headers={'Origin': 'http://evil'}, json={})
        self.assertEqual(response.status_code, 403)

    async def test_attachment_proxy_streams_bytes_without_returning_credentials(self):
        seen = []
        async def handler(request):
            seen.append((str(request.url), request.headers['authorization'], await request.aread()))
            return httpx.Response(200, json={'path': '/config/uploads/中文.txt', 'size': 7, 'filename': '中文.txt'})
        remote = WeHarbor({'origin': 'http://127.0.0.1:8766', 'token': 'private-token'}, httpx.MockTransport(handler))
        with patch.object(advanced, 'WeHarbor', return_value=remote):
            response = await self.client.put('/api/advanced/attachments/中文.txt', content=b'fixture')
            denied = await self.client.put('/api/advanced/attachments/secret.txt', content=b'x', headers={'Origin': 'https://foreign.test'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(seen, [('http://127.0.0.1:8766/attachments/%E4%B8%AD%E6%96%87.txt', 'Bearer private-token', b'fixture')])
        self.assertNotIn('private-token', response.text)
        self.assertEqual(denied.status_code, 403)

    async def test_attachment_proxy_rejects_empty_oversize_and_invalid_names(self):
        async def handler(request):
            await request.aread()
            return httpx.Response(200, json={})
        remote = WeHarbor({'origin': 'http://127.0.0.1:8766', 'token': ''}, httpx.MockTransport(handler))
        with patch.object(advanced, 'WeHarbor', return_value=remote):
            self.assertEqual((await self.client.put('/api/advanced/attachments/empty', content=b'')).status_code, 400)
            self.assertEqual((await self.client.put('/api/advanced/attachments/large', content=b'x', headers={'Content-Length': str(100 * 1024 * 1024 + 1)})).status_code, 413)
            self.assertEqual((await self.client.put('/api/advanced/attachments/bad%5Cname', content=b'x')).status_code, 400)

    async def test_mcp_auth_and_discovered_read_action_task_tools(self):
        payload = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}
        denied = await self.client.post('/api/advanced/mcp', json=payload)
        self.assertEqual(denied.status_code, 401)
        path = store.state_dir() / 'mcp.token'
        original = path.read_text()
        path.chmod(0o705)  # NAS inherited permissions must be tightened on reuse.
        token = advanced.mcp_token()
        self.assertEqual(path.read_text(), original)
        response = await self.client.post('/api/advanced/mcp', json=payload, headers={'Authorization': 'Bearer ' + token})
        tools = response.json()['result']['tools']
        self.assertEqual(len(tools), 19)
        self.assertEqual(sum(t['annotations']['readOnlyHint'] for t in tools), 12)
        self.assertTrue({'wechat_timeline', 'wechat_prepare_action', 'wechat_commit_action', 'wechat_tasks', 'wechat_create_task'} <= {t['name'] for t in tools})
        self.assertEqual((store.state_dir() / 'mcp.token').stat().st_mode & 0o777, 0o600)

    async def test_mcp_rejects_write_tool_and_invalid_pagination(self):
        for name, args in [('wechat_send_text', {}), ('wechat_messages', {'account': 'a', 'username': 'b', 'limit': True})]:
            response = await self.client.post('/api/advanced/mcp', headers={'Authorization': 'Bearer ' + advanced.mcp_token()},
                json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': name, 'arguments': args}})
            self.assertEqual(response.json()['error']['code'], -32602)

    async def rpc(self, method, params=None):
        response = await self.client.post('/api/advanced/mcp', headers={'Authorization': 'Bearer ' + advanced.mcp_token()},
            json={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}})
        self.assertEqual(response.status_code, 200)
        return response.json()

    async def test_mcp_skill_and_authorized_action_share_web_services(self):
        initialized = await self.rpc('initialize')
        self.assertIn('resources', initialized['result']['capabilities'])
        skill = await self.rpc('resources/read', {'uri': 'wechat-analysis://skill'})
        self.assertIn('automation-moments-sync', skill['result']['contents'][0]['text'])
        arguments = {'feature': 'send-text', 'payload': {'account': 'self', 'username': 'friend', 'text': 'approved'}, 'idempotency_key': 'stable'}
        with patch.object(service, 'prepare', new=AsyncMock(return_value={'id': 'stable', 'status': 'prepared'})) as prepare, \
             patch.object(service, 'commit', new=AsyncMock(return_value={'status': 'sent'})) as commit:
            await self.rpc('tools/call', {'name': 'wechat_prepare_action', 'arguments': arguments})
            prepare.assert_awaited_once_with('send-text', arguments['payload'], 'stable')
            commit.assert_not_awaited()
            result = await self.rpc('tools/call', {'name': 'wechat_commit_action', 'arguments': {'action_id': 'stable'}})
            commit.assert_awaited_once_with('stable')
            self.assertEqual(json.loads(result['result']['content'][0]['text'])['status'], 'sent')

    async def test_mcp_task_draft_and_control_validate_and_report_errors(self):
        with patch.object(tasks, 'create', new=AsyncMock(return_value={'id': 'draft', 'status': 'draft'})) as create, \
             patch.object(tasks, 'operate', new=AsyncMock(side_effect=ValueError('任务不存在'))) as operate:
            plan = {'kind': 'automation-moments-sync', 'account': 'self'}
            await self.rpc('tools/call', {'name': 'wechat_create_task', 'arguments': {'plan': plan}})
            create.assert_awaited_once_with(plan)
            operate.assert_not_awaited()
            for args in [{'task_id': 1, 'operation': 'activate'}, {'task_id': 'draft', 'operation': 'delete'}, {'task_id': 'draft', 'operation': 'activate', 'extra': True}]:
                invalid = await self.rpc('tools/call', {'name': 'wechat_control_task', 'arguments': args})
                self.assertEqual(invalid['error']['code'], -32602)
            result = await self.rpc('tools/call', {'name': 'wechat_control_task', 'arguments': {'task_id': 'draft', 'operation': 'activate'}})
            self.assertTrue(result['result']['isError'])
            self.assertIn('任务不存在', result['result']['content'][0]['text'])

    async def test_mcp_timeline_maps_exact_username_filter(self):
        seen = []
        @self.app.get('/api/sns/timeline')
        async def timeline(usernames: str, source: str):
            seen.append((usernames, source))
            return {'timeline': []}
        result = await self.rpc('tools/call', {'name': 'wechat_timeline', 'arguments': {'account': 'self', 'username': 'exact'}})
        self.assertNotIn('isError', result['result'])
        self.assertEqual(seen, [('exact', 'decrypted')])

    async def test_bridge_uses_weharbor_workspace_api_and_bearer(self):
        seen = []
        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={'jsonrpc': '2.0', 'id': 1, 'result': {'tools': []}})
        client = WeHarbor({'origin': 'https://example.test', 'token': 'secret'}, httpx.MockTransport(handler))
        self.assertEqual(await client.tools(), [])
        self.assertEqual(str(seen[0].url), 'https://example.test/workspace/mcp')
        self.assertEqual(seen[0].headers['authorization'], 'Bearer secret')

    async def test_bridge_rejects_malformed_results_and_redirects(self):
        for status, payload in [(200, []), (200, {'result': []}), (302, {})]:
            with self.subTest(status=status, payload=payload):
                client = WeHarbor({'origin': 'http://localhost:8766', 'token': ''},
                    httpx.MockTransport(lambda request: httpx.Response(status, json=payload)))
                with self.assertRaises(BridgeError):
                    await client.tools()


if __name__ == '__main__':
    unittest.main()
