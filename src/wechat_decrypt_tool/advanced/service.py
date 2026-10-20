"""Capability checked actions with durable, at-most-once submission receipts."""
import asyncio
import json
import hashlib
import time
import uuid
from .store import connect, config
from .weharbor import WeHarbor, BridgeError
from .editing import EDIT_KEYS, TYPES

ONLINE = '''send-text send-at send-image send-video send-emoji send-file send-link send-voice send-pat chat-mark-read chat-set-mute send-location send-contact-card send-quote send-favorite-emoji receive-messages profile-qrcode sns-autorefresh sns-like sns-image-comment sns-text-comment sns-post sns-delete-post sns-delete-comment group-my-nick group-notice group-create group-rename group-add-members group-invite-members group-remove-members group-leave group-add-friends contact-remark contact-accept contact-delete contact-add contact-create-label contact-set-labels contact-list-labels contact-rename-label contact-delete-label contact-search contact-insights search-article search-official-account search-channel-account search-channel-video red-packet-detail'''.split()
AUTOMATION = ['automation-broadcast', 'automation-onboarding', 'automation-moments-sync']
FEATURES = [*EDIT_KEYS, *['add-' + k for k in TYPES], 'ai-assistant', 'mcp-access', *ONLINE, *AUTOMATION]
READS = {'receive-messages': 'chat/messages'}
ACTION_LOCK = asyncio.Lock()
DESKTOP_FEATURES = {'send-file', 'send-image', 'send-video', 'chat-mark-read', 'chat-set-mute',
                    'contact-remark', 'group-rename', 'group-my-nick', 'group-notice',
                    'contact-create-label', 'contact-set-labels', 'contact-rename-label', 'contact-delete-label'}
TOOL_READS = {'contact-list-labels'}
IMPLEMENTED = {*EDIT_KEYS, *['add-' + k for k in TYPES], 'ai-assistant', 'mcp-access',
               *READS, *DESKTOP_FEATURES, *TOOL_READS, *AUTOMATION, 'send-text', 'contact-insights', 'sns-autorefresh'}
REMOTE_STATES = {'prepared', 'scheduled', 'sent', 'completed', 'uncertain', 'failed', 'expired', 'cancelled'}


def remote_status(result):
    """An MCP transport success does not imply a successful WeChat action."""
    value = result
    content = result.get('content') if isinstance(result, dict) else None
    if isinstance(content, list) and len(content) == 1 and isinstance(content[0], dict) and content[0].get('type') == 'text':
        try:
            value = json.loads(content[0]['text'])
        except (ValueError, KeyError, TypeError):
            pass
    if isinstance(value, dict) and 'status' in value:
        return value['status'] if isinstance(value['status'], str) and value['status'] in REMOTE_STATES else 'uncertain'
    return 'completed'


def provider(settings=None):
    settings = config() if settings is None else settings
    return hashlib.sha256((settings['origin'] + '\n' + settings['token']).encode()).hexdigest()


def tool_name(feature, tools):
    names = {t['name'] for t in tools}
    explicit = config()['tool_bindings'].get(feature)
    candidates = [explicit] if explicit else ['wechat_' + feature.replace('-', '_')]
    return next((n for n in candidates if n in names), None)


async def capabilities():
    bridge = WeHarbor()
    error = ''
    try:
        tools = await bridge.tools()
    except BridgeError as exc:
        tools, error = [], str(exc)
    names = {t['name'] for t in tools}
    result = {}
    for feature in FEATURES:
        state, reason, schema = 'available', '', None
        if feature in ONLINE and feature not in READS and feature != 'contact-insights':
            tool = tool_name(feature, tools)
            if feature == 'send-text' and {'wechat_prepare_send', 'wechat_commit_send'} <= names:
                pass
            elif tool:
                definition = next(t for t in tools if t['name'] == tool)
                schema = definition.get('inputSchema', {})
                if feature in TOOL_READS and definition.get('annotations', {}).get('readOnlyHint') is not True:
                    state, reason = 'blocked', '标签查询工具必须声明只读，不会通过查询入口调用写入工具'
            else:
                state, reason = 'blocked', error or '当前 WeHarbor 未提供此操作的驱动；需要实际实现并发布对应 MCP 工具'
        if feature in AUTOMATION:
            needed = {'automation-broadcast': ['send-text'],
                      'automation-onboarding': ['chat-mark-read', 'contact-remark', 'contact-set-labels', 'send-text'],
                      'automation-moments-sync': ['sns-autorefresh']}[feature]
            missing = [key for key in needed if result.get(key, {}).get('state') != 'available']
            if missing:
                state, reason = 'blocked', '缺少执行能力：' + '、'.join(missing)
        if feature.startswith('edit-') or feature.startswith('add-'):
            reason = '修改分析用解密副本；不会回写微信客户端，重新同步可能覆盖'
        implementation = 'implemented' if feature in IMPLEMENTED or state == 'available' else 'pending'
        if state == 'blocked' and implementation == 'implemented':
            reason = ('WeHarbor 连接不可用，请检查连接设置' if error else '代码已实现；当前实例尚未发布所需工具，请更新 WeHarbor') if feature not in AUTOMATION else reason
        result[feature] = {'state': state, 'implementation': implementation, 'reason': reason, 'schema': schema, 'unlocked': True}
    return {'features': result, 'bridge_error': error, 'tools': tools,
            'counts': {'total': len(FEATURES), 'available': sum(x['state'] == 'available' for x in result.values())}}


async def refresh_moments(account):
    if not isinstance(account, str) or not account.strip():
        raise ValueError('需要明确账户')
    bridge = WeHarbor()
    tools = await bridge.tools()
    name = tool_name('sns-autorefresh', tools)
    if not name:
        raise ValueError('当前 WeHarbor 未发布朋友圈刷新工具，请更新实例')
    result = await bridge.call(name, {'account': account})
    if remote_status(result) not in ('completed', 'sent'):
        raise ValueError('朋友圈刷新未完成，请检查 WeHarbor 回执')
    return result


def receipt(identifier):
    with connect() as db:
        row = db.execute('SELECT * FROM receipts WHERE id=?', (identifier,)).fetchone()
    if row is None:
        raise ValueError('操作不存在')
    return dict(row) | {'payload': json.loads(row['payload']), 'result': json.loads(row['result'])}


def update(identifier, status, result):
    with connect() as db:
        db.execute('UPDATE receipts SET status=?,result=? WHERE id=?', (status, json.dumps(result, ensure_ascii=False), identifier))
    return receipt(identifier)


def history():
    with connect() as db:
        identifiers = [r[0] for r in db.execute('SELECT id FROM receipts ORDER BY created DESC LIMIT 100')]
    return [receipt(identifier) for identifier in identifiers]


async def prepare(feature, payload, identifier):
    if feature not in ONLINE or feature in READS or feature in TOOL_READS or feature == 'contact-insights':
        raise ValueError('该功能不使用在线写入流程')
    if not isinstance(payload, dict):
        raise ValueError('payload 必须是对象')
    if not isinstance(identifier, str) or not identifier or len(identifier) > 128:
        raise ValueError('需要不超过 128 字符的幂等键')
    async with ACTION_LOCK:
        with connect() as db:
            existing = db.execute('SELECT id FROM receipts WHERE id=?', (identifier,)).fetchone()
        if existing:
            old = receipt(identifier)
            if old['feature'] != feature or old['payload'] != payload:
                raise ValueError('幂等键已用于其他内容')
            return old
        bridge = WeHarbor()
        binding = provider(bridge.settings)
        tools = await bridge.tools()
        names = {t['name'] for t in tools}
        if feature == 'send-text' and {'wechat_prepare_send', 'wechat_commit_send'} <= names:
            if not all(isinstance(payload.get(k), str) and payload[k].strip() for k in ('account', 'username', 'text')):
                raise ValueError('需要账户、精确 username 和文本')
            if len(payload['text']) > 4000:
                raise ValueError('文本超过 4000 字符')
            mode = 'text'
        else:
            name = tool_name(feature, tools)
            if not name:
                raise ValueError('WeHarbor 尚未实现此操作，未发送任何请求')
            parameters = payload.get('parameters')
            if not isinstance(parameters, dict):
                raise ValueError('parameters 必须是 MCP 工具参数对象')
            definition = next(t for t in tools if t['name'] == name)
            schema = definition.get('inputSchema', {})
            arguments = dict(parameters)
            for key in ('account', 'username'):
                if key in schema.get('properties', {}) and key in arguments and key in payload and arguments[key] != payload[key]:
                    raise ValueError('工具参数中的账户或目标与当前操作不一致')
                if key in schema.get('properties', {}) and key not in arguments and key in payload:
                    arguments[key] = payload[key]
            if 'idempotency_key' in schema.get('properties', {}):
                if arguments.get('idempotency_key', identifier) != identifier:
                    raise ValueError('工具幂等键必须与当前操作一致，不能另设重试键')
                arguments['idempotency_key'] = identifier
            if set(schema.get('required', [])) - set(arguments):
                raise ValueError('缺少工具必填参数，请参考参数定义')
            native = name == 'wechat_' + feature.replace('-', '_')
            mode = 'desktop' if native and feature in DESKTOP_FEATURES and {'wechat_prepare_operation', 'wechat_commit_operation'} <= names else name
        with connect() as db:
            db.execute('INSERT INTO receipts VALUES (?,?,?,?,?,?)',
                       (identifier, feature, json.dumps(payload, ensure_ascii=False), 'preparing', json.dumps({'mode': mode, 'provider': binding}), time.time()))
        if mode in ('text', 'desktop'):
            try:
                if mode == 'text':
                    body = {k: payload[k] for k in ('account', 'username', 'text')}
                    if payload.get('due') is not None:
                        body['due'] = payload['due']
                    path = '/actions/prepare'
                else:
                    body = {'operation': feature, 'account': arguments.get('account'), 'username': arguments.get('username'),
                            'parameters': {k: v for k, v in arguments.items() if k not in ('account', 'username', 'idempotency_key')}}
                    path = '/operations/prepare'
                result = await bridge.request('POST', path, json=body | {'idempotency_key': identifier})
                if not isinstance(result.get('id'), str):
                    raise BridgeError('WeHarbor 未返回可提交的操作 ID')
                details = {'mode': mode, 'provider': binding, 'remote': result}
                if mode == 'desktop':
                    details.update(tool=name, tool_schema=schema)
                return update(identifier, remote_status(result), details)
            except BaseException:
                # Recover the same idempotency key only through remote receipts.
                update(identifier, 'uncertain', {'mode': mode, 'provider': binding, 'reason': '准备请求中断；请核对 WeHarbor 操作记录'})
                raise
        return update(identifier, 'prepared', {'mode': mode, 'provider': binding, 'tool_schema': definition.get('inputSchema', {}), 'arguments': arguments})


async def read_labels(account, username):
    if not all(isinstance(v, str) and v.strip() for v in (account, username)):
        raise ValueError('需要明确账户与作为标签入口的联系人 username')
    bridge = WeHarbor()
    tools = await bridge.tools()
    name = tool_name('contact-list-labels', tools)
    definition = next((t for t in tools if t['name'] == name), None)
    if not definition or definition.get('annotations', {}).get('readOnlyHint') is not True:
        raise ValueError('实例未提供只读标签列表工具')
    return await bridge.call(name, {'account': account, 'username': username})


async def commit(identifier):
    async with ACTION_LOCK:
        item = receipt(identifier)
        if item['status'] != 'prepared':
            return item
        if time.time() - item['created'] > 120:
            return update(identifier, 'expired', item['result'])
        bridge = WeHarbor()
        if item['result'].get('provider') != provider(bridge.settings):
            return update(identifier, 'cancelled', item['result'] | {'reason': 'WeHarbor 连接配置已变化，请重新准备'})
        mode = item['result']['mode']
        update(identifier, 'submitting', item['result'])
        try:
            if mode == 'text':
                remote = await bridge.request('POST', '/actions/' + item['result']['remote']['id'] + '/commit')
                status = remote.get('status', 'uncertain')
                return update(identifier, status, item['result'] | {'remote': remote})
            if mode == 'desktop':
                tools = await bridge.tools()
                current = next((t for t in tools if t['name'] == item['result']['tool']), None)
                if not current or current.get('inputSchema', {}) != item['result']['tool_schema'] or tool_name(item['feature'], tools) != item['result']['tool']:
                    await bridge.request('POST', '/operations/' + item['result']['remote']['id'] + '/cancel')
                    return update(identifier, 'cancelled', item['result'] | {'reason': '工具定义已变化，请重新准备'})
                remote = await bridge.request('POST', '/operations/' + item['result']['remote']['id'] + '/commit')
                return update(identifier, remote_status(remote), item['result'] | {'remote': remote})
            # Tool names and schemas are checked again; a reconfiguration cannot
            # silently redirect an already prepared action.
            tools = await bridge.tools()
            current = next((t for t in tools if t['name'] == mode), None)
            if not current or current.get('inputSchema', {}) != item['result']['tool_schema'] or tool_name(item['feature'], tools) != mode:
                return update(identifier, 'cancelled', {'reason': '工具配置或参数定义已变化，请重新准备'})
            result = await bridge.call(mode, item['result'].get('arguments', item['payload']['parameters']))
            # Generic tools do not establish delivery or DB receipts.
            return update(identifier, remote_status(result), item['result'] | {'response': result})
        except BaseException:
            update(identifier, 'uncertain', item['result'] | {'reason': '请求中断或远程执行失败；请核对操作结果，不能自动重试'})
            raise


async def cancel(identifier):
    async with ACTION_LOCK:
        item = receipt(identifier)
        if item['status'] not in ('prepared', 'scheduled'):
            raise ValueError('仅能取消待提交或待执行的操作')
        bridge = WeHarbor()
        if item['result'].get('provider') != provider(bridge.settings):
            raise ValueError('WeHarbor 连接配置已变化，请在原服务取消该操作')
        if item['result'].get('mode') in ('text', 'desktop'):
            path = '/actions/' if item['result']['mode'] == 'text' else '/operations/'
            remote = await bridge.request('POST', path + item['result']['remote']['id'] + '/cancel')
            return update(identifier, remote['status'], item['result'] | {'remote': remote})
        return update(identifier, 'cancelled', item['result'])


async def reconcile():
    """Read remote scheduled receipts; this never repeats a send."""
    with connect() as db:
        db.execute("UPDATE receipts SET status='uncertain' WHERE status IN ('preparing','submitting')")
    while True:
        with connect() as db:
            identifiers = [r[0] for r in db.execute("SELECT id FROM receipts WHERE status IN ('scheduled','uncertain')")]
        pending = [receipt(key) for key in identifiers]
        bridge = WeHarbor()
        pending = [r for r in pending if r['result'].get('provider') == provider(bridge.settings)]
        if pending:
            try:
                remote = await bridge.request('GET', '/actions')
                lookup = {r['id']: r for r in remote.get('actions', [])}
                for item in pending:
                    match = lookup.get(item['result'].get('remote', {}).get('id'))
                    if not match:
                        match = next((r for r in remote.get('actions', []) if r.get('idempotency_key') == item['id']), None)
                    if match and match['status'] not in ('prepared', 'submitted'):
                        update(item['id'], match['status'], item['result'] | {'remote': match})
            except BridgeError:
                pass
        await asyncio.sleep(10)
