"""Harness-facing actions use the same durable service as the web workbench."""
import math
from . import editing, service, tasks


def tool(name, description, properties=None, required=None, readonly=True):
    properties = properties or {}
    return {'name': 'wechat_' + name, 'description': description,
        'inputSchema': {'type': 'object', 'properties': properties,
            'required': list(properties) if required is None else required, 'additionalProperties': False},
        'annotations': {'readOnlyHint': readonly, 'destructiveHint': not readonly,
                        'idempotentHint': readonly}}


STRING = {'type': 'string', 'minLength': 1}
OBJECT = {'type': 'object'}
TOOLS = [
    tool('capabilities', 'List unlocked features and actual WeHarbor driver availability. Check before acting.'),
    tool('prepare_action', 'Prepare the exact feature and payload for review; never sends. Text payload: account, username, text, optional due Unix seconds. Other operations: account, username, parameters matching discovered schema. Reuse the same idempotency_key for the same intent.',
         {'feature': STRING, 'payload': OBJECT, 'idempotency_key': STRING}, readonly=False),
    tool('commit_action', 'Submit a previously reviewed action once. Requires user authorization for its exact recipient and content. Never retry uncertain results with a new key.', {'action_id': STRING}, readonly=False),
    tool('cancel_action', 'Cancel an unsubmitted action.', {'action_id': STRING}, readonly=False),
    tool('action_receipts', 'Read persistent action receipts; sent is a database observation, not recipient delivery.'),
    tool('tasks', 'List durable automation drafts, status, progress and archive output paths.'),
    tool('create_task', 'Create an inactive draft. kind: automation-broadcast (send-text/image/video/file, optional chat-mark-read), automation-onboarding, or automation-moments-sync (no interaction steps; interval >=30; format html/json). plan includes account, targets, interval, optional due; messaging tasks require steps [{feature,payload}]. Creating never activates.', {'plan': OBJECT}, readonly=False),
    tool('control_task', 'Activate only the user-authorized draft; pause, resume or cancel a task. Continuous Moments sync only refreshes and archives, never replies, likes, comments or reposts.',
         {'task_id': STRING, 'operation': {'type': 'string', 'enum': ['activate', 'pause', 'resume', 'cancel']}}, readonly=False),
    tool('preview_edit', 'Preview edits to the analysis decrypted copy only. payload includes feature, account, username, message_id or batch bounds. Retain the returned revision.', {'payload': OBJECT}),
    tool('apply_edit', 'Apply an authorized edit to the decrypted analysis copy, using the preview revision. Never writes to the live WeChat client.', {'payload': OBJECT}, readonly=False),
    tool('labels', 'Read the complete current label list; requires an exact open anchor chat.', {'account': STRING, 'username': STRING}),
    tool('refresh_moments', 'Request one native Moments refresh and snapshot sync for the exact local account. Does not start a continuous task or post any interaction.', {'account': STRING}, readonly=False),
]


def validate(value, schema):
    kind = schema.get('type')
    correct = {'object': isinstance(value, dict), 'string': isinstance(value, str),
               'integer': type(value) is int, 'number': type(value) in (int, float),
               'boolean': type(value) is bool, 'array': isinstance(value, list)}
    if kind and not correct.get(kind, False):
        raise ValueError('工具参数类型不正确')
    if type(value) in (int, float) and not math.isfinite(value):
        raise ValueError('数值必须有限')
    if 'enum' in schema and value not in schema['enum']:
        raise ValueError('不支持的操作')
    if kind == 'object':
        properties = schema.get('properties', {})
        if set(schema.get('required', [])) - set(value) or (schema.get('additionalProperties') is False and set(value) - set(properties)):
            raise ValueError('工具参数缺失或含未知字段')
        for key in set(value) & set(properties):
            validate(value[key], properties[key])
    if kind == 'string' and len(value) < schema.get('minLength', 0):
        raise ValueError('工具参数不能为空')


async def call(name, args, app):
    if name == 'wechat_capabilities': return await service.capabilities()
    if name == 'wechat_prepare_action': return await service.prepare(args['feature'], args['payload'], args['idempotency_key'])
    if name == 'wechat_commit_action': return await service.commit(args['action_id'])
    if name == 'wechat_cancel_action': return await service.cancel(args['action_id'])
    if name == 'wechat_action_receipts': return {'actions': service.history()}
    if name == 'wechat_tasks': return {'tasks': tasks.history()}
    if name == 'wechat_create_task': return await tasks.create(args['plan'])
    if name == 'wechat_control_task': return await tasks.operate(args['task_id'], args['operation'], app)
    if name == 'wechat_preview_edit':
        import asyncio
        return await asyncio.to_thread(editing.preview, args['payload'])
    if name == 'wechat_apply_edit':
        import asyncio
        return await asyncio.to_thread(editing.apply, args['payload'])
    if name == 'wechat_labels': return await service.read_labels(args['account'], args['username'])
    if name == 'wechat_refresh_moments': return await service.refresh_moments(args['account'])
    raise ValueError('未知工具')
