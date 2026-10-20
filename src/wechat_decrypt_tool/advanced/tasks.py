"""Durable explicit-action queues and bounded new-contact/Moments triggers.

Preparation creates a draft; activation authorizes its exact configured steps.
An ambiguous result pauses the queue. Restart never repeats a submitted action.
"""
import asyncio
import json
import math
import time
import uuid
from types import SimpleNamespace
from . import service, moments
from .store import connect

LOCK = asyncio.Lock()
KINDS = {'automation-broadcast', 'automation-onboarding', 'automation-moments-sync'}
LEGACY_MOMENTS = 'automation-moments-follow'


def get(identifier):
    with connect() as db:
        row = db.execute('SELECT * FROM tasks WHERE id=?', (identifier,)).fetchone()
    if not row:
        raise ValueError('任务不存在')
    return dict(row) | {'value': json.loads(row['value'])}


def save(task):
    with connect() as db:
        db.execute('UPDATE tasks SET value=?,status=?,cursor=?,due=?,result=? WHERE id=?',
                   (json.dumps(task['value'], ensure_ascii=False), task['status'], task['cursor'], task['due'], task['result'], task['id']))


def history():
    with connect() as db:
        identifiers = [r[0] for r in db.execute('SELECT id FROM tasks ORDER BY rowid DESC LIMIT 100')]
    return [get(identifier) for identifier in identifiers]


def substitute(value, context):
    if isinstance(value, str):
        return context.get(value, value)
    if isinstance(value, dict):
        return {k: substitute(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute(v, context) for v in value]
    return value


async def create(value):
    kind = value.get('kind')
    if kind not in KINDS or not isinstance(value.get('account'), str) or not value['account']:
        raise ValueError('需要任务类型和账户')
    syncing = kind == 'automation-moments-sync'
    steps = value.get('steps', [] if syncing else None)
    if syncing and steps != []:
        raise ValueError('朋友圈连续同步只刷新并保存，不能包含互动步骤')
    if not isinstance(steps, list) or (not syncing and not 1 <= len(steps) <= 20):
        raise ValueError('每个目标需要 1 至 20 个操作步骤')
    allowed = {'automation-broadcast': {'chat-mark-read', 'send-text', 'send-image', 'send-video', 'send-file'},
               'automation-onboarding': {'chat-mark-read', 'contact-remark', 'contact-set-labels', 'send-text'},
               'automation-moments-sync': set()}[kind]
    caps = (await service.capabilities())['features']
    if syncing and caps['sns-autorefresh']['state'] != 'available':
        raise ValueError(caps['sns-autorefresh']['reason'])
    if syncing and value.get('format', 'html') not in ('html', 'json'):
        raise ValueError('连续同步保存格式必须是 html 或 json')
    for step in steps:
        if not isinstance(step, dict) or step.get('feature') not in allowed or not isinstance(step.get('payload'), dict):
            raise ValueError('任务步骤类型或参数不正确')
        if caps[step['feature']]['state'] != 'available':
            raise ValueError(caps[step['feature']]['reason'])
        parameters = step['payload'].get('parameters', {})
        if not isinstance(parameters, dict) or parameters.get('account', '$account') not in ('$account', value['account']):
            raise ValueError('任务步骤不能切换账户')
    targets = value.get('targets', [])
    if not isinstance(targets, list) or any(not isinstance(t, str) or not t.strip() for t in targets) or len(set(targets)) != len(targets):
        raise ValueError('targets 必须是不重复的精确 username 列表')
    if kind == 'automation-broadcast' and not 1 <= len(targets) <= 200:
        raise ValueError('一次群发需要 1 至 200 个目标')
    for field in ('keywords', 'blocked_words'):
        words = value.get(field, [])
        if not isinstance(words, list) or any(not isinstance(w, str) or not w.strip() for w in words):
            raise ValueError(field + ' 必须是非空字符串列表')
    interval = value.get('interval', 30)
    due = value.get('due', time.time())
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not (30 if syncing else 5) <= interval <= 86400:
        raise ValueError('interval 必须是 %s 至 86400 秒' % (30 if syncing else 5))
    if isinstance(due, bool) or not isinstance(due, (int, float)) or not math.isfinite(due) or not 0 < due <= time.time() + 366 * 86400:
        raise ValueError('due 必须是下一年以内的 Unix 秒数')
    plan = {'kind': kind, 'account': value['account'], 'steps': steps}
    if syncing:
        plan['format'] = value.get('format', 'html')
    plan.update(targets=targets, interval=interval, keywords=value.get('keywords', []),
                blocked_words=value.get('blocked_words', []), seen=[], queue=[], activated=0)
    identifier = uuid.uuid4().hex
    with connect() as db:
        db.execute('INSERT INTO tasks(id,value,status,cursor,due,result) VALUES (?,?,?,0,?,?)',
                   (identifier, json.dumps(plan, ensure_ascii=False), 'draft', due, '等待确认启动'))
    return get(identifier)


async def source(app, plan):
    from ..routers.advanced import local_read
    name = 'contacts' if plan['kind'] == 'automation-onboarding' else 'timeline'
    params = {'account': plan['account']} | ({'limit': 200} if name == 'timeline' else {})
    data = await local_read(SimpleNamespace(app=app), name, params)
    items = data.get('contacts' if name == 'contacts' else 'timeline', [])
    if name == 'contacts':
        if not isinstance(items, list) or type(data.get('total')) is not int or data['total'] != len(items) or data.get('account', plan['account']) != plan['account']:
            raise ValueError('迎新需要完整且账户一致的通讯录快照，拒绝从部分结果触发')
        names = [item.get('username') for item in items if isinstance(item, dict)]
        if len(names) != len(items) or any(not isinstance(v, str) or not v.strip() for v in names) or len(set(names)) != len(names):
            raise ValueError('通讯录身份不完整或存在重复 username')
        items = [item for item in items if item.get('type') == 'friend' and item.get('username') != plan['account']]
    return items


def event_id(plan, item):
    return str(item.get('username') or '') if plan['kind'] == 'automation-onboarding' else str(item.get('id') or '')


def enqueue(plan, context):
    for step in plan['steps']:
        payload = substitute(step['payload'], context)
        # A task cannot switch accounts through a placeholder or payload.
        payload['account'] = plan['account']
        if isinstance(payload.get('parameters'), dict) and 'account' in payload['parameters']:
            payload['parameters']['account'] = plan['account']
        if step['feature'] != 'sns-post':
            payload['username'] = context['$username']
            if isinstance(payload.get('parameters'), dict) and 'username' in payload['parameters']:
                payload['parameters']['username'] = context['$username']
        plan['queue'].append({'feature': step['feature'], 'payload': payload})


async def operate(identifier, operation, app):
    async with LOCK:
        task = get(identifier)
        if task['value']['kind'] == LEGACY_MOMENTS and operation in ('activate', 'resume'):
            raise ValueError('朋友圈互动任务已停用；请新建朋友圈连续同步任务')
        if operation == 'activate' and task['status'] == 'draft':
            plan = task['value']
            if plan['kind'] == 'automation-broadcast':
                for username in plan['targets']:
                    enqueue(plan, {'$account': plan['account'], '$username': username})
            elif plan['kind'] == 'automation-onboarding':
                items = await source(app, plan)
                plan['seen'] = [event_id(plan, item) for item in items if event_id(plan, item)]
            plan['activated'] = time.time()
            plan['provider'] = service.provider()
            task['status'], task['result'] = 'active', '已授权并启动'
        elif operation == 'pause' and task['status'] == 'active':
            task['status'], task['result'] = 'paused', '已暂停'
            if task['value']['kind'] == 'automation-moments-sync':
                moments.cancel_export(task['value'])
        elif operation == 'resume' and task['status'] == 'paused':
            task['status'], task['result'] = 'active', '已继续；不会重试不确定的提交'
        elif operation == 'cancel' and task['status'] in ('draft', 'active', 'paused'):
            task['status'], task['result'] = 'cancelled', '已取消未执行的步骤'
            if task['value']['kind'] == 'automation-moments-sync':
                moments.cancel_export(task['value'])
        else:
            raise ValueError('当前任务状态不允许此操作')
        save(task)
        return task


async def tick(app, identifier):
    async with LOCK:
        task = get(identifier)
        if task['status'] != 'active':
            return
        plan = task['value']
        if plan['kind'] == LEGACY_MOMENTS:
            task['status'], task['result'] = 'paused', '朋友圈互动任务已停用；请新建连续同步任务'
            save(task)
            return
        if task['due'] > time.time():
            return
        if plan.get('provider') != service.provider():
            task['status'], task['result'] = 'paused', 'WeHarbor 连接配置已变化；请核对后重新创建任务'
            save(task)
            return
        if plan['kind'] == 'automation-moments-sync':
            await moments.tick(task)
            save(task)
            return
        if task['cursor'] >= len(plan['queue']):
            if plan['kind'] == 'automation-broadcast':
                task['status'], task['result'] = 'completed', '所有步骤已有成功回执'
                save(task)
                return
            items = await source(app, plan)
            seen = set(plan['seen'])
            for item in items:
                key = event_id(plan, item)
                if not key or key in seen:
                    continue
                seen.add(key)
                username = str(item.get('username') or '')
                text = str(item.get('contentDesc') or '')
                if plan['kind'] == 'automation-onboarding' and plan['targets'] and username not in plan['targets']:
                    continue
                enqueue(plan, {'$account': plan['account'], '$username': username, '$post_id': key,
                               '$content': text, '$media': item.get('media', [])})
            plan['seen'] = sorted(seen)
            save(task)
        if task['cursor'] < len(plan['queue']):
            step = plan['queue'][task['cursor']]
            key = 'task-' + identifier + '-' + str(task['cursor'])
            prepared = await service.prepare(step['feature'], step['payload'], key)
            result = await service.commit(prepared['id'])
            if result['status'] in ('sent', 'completed'):
                task['cursor'] += 1
                task['result'] = '已完成 %s 个步骤' % task['cursor']
            else:
                task['status'] = 'paused'
                task['result'] = '步骤 %s 状态 %s；请核对回执。不会自动重试。' % (task['cursor'] + 1, result['status'])
        task['due'] = time.time() + plan['interval']
        save(task)


async def scheduler(app):
    while True:
        with connect() as db:
            identifiers = [r[0] for r in db.execute("SELECT id FROM tasks WHERE status='active' ORDER BY due")]
        for identifier in identifiers:
            try:
                await tick(app, identifier)
            except Exception:
                async with LOCK:
                    current = get(identifier)
                    if current['status'] == 'active':
                        if current['value']['kind'] == 'automation-moments-sync':
                            current['result'] = '本轮同步失败，已保存内容保留；稍后自动重试，请检查连接与微信桌面'
                            current['due'] = time.time() + max(30, current['value']['interval'])
                        else:
                            current['status'], current['result'] = 'paused', '执行失败，请检查连接、驱动和操作回执后处理'
                        save(current)
        await asyncio.sleep(2)
