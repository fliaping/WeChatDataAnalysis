"""Continuous native refresh and restart-safe incremental Moments exports."""
import asyncio
import os
import time
from . import service
from .store import state_dir


def manager():
    # Import lazily: ordinary chats and MCP discovery don't need SNS exporters.
    from ..sns_export_service import SNS_EXPORT_MANAGER
    return SNS_EXPORT_MANAGER


def output_directory(identifier):
    if len(identifier) != 32 or any(c not in '0123456789abcdef' for c in identifier):
        raise ValueError('任务标识不正确')
    root = state_dir() / 'moments' / identifier
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    return root


async def tick(task):
    plan = task['value']
    exports = manager()
    job = exports.get_job(plan['export_id']) if plan.get('export_id') else None
    if job is not None and job.status in ('queued', 'running'):
        task['result'] = '正在增量保存朋友圈内容与媒体'
        task['due'] = time.time() + 2
        return
    if job is not None:
        plan['last_export'] = job.to_public_dict()
        plan.pop('export_id', None)
        if job.status == 'done':
            task['cursor'] += 1
            plan['last_saved_at'] = time.time()
            task['result'] = '已完成 %s 轮同步并保存；%s' % (task['cursor'], job.folder_path or '')
            if job.warning:
                task['result'] += '；' + job.warning
        else:
            task['result'] = '本轮保存未完成，将按间隔重试；已保存内容保留'
        task['due'] = time.time() + plan['interval']
        exports.release_job(job.export_id)
        return
    # A job lost during restart can safely be recreated: the exporter reads its
    # on-disk baseline, writes files atomically and retries missing media.
    plan.pop('export_id', None)
    plan['last_refresh'] = await service.refresh_moments(plan['account'])
    root = output_directory(task['id'])
    job = await asyncio.to_thread(exports.create_job, account=plan['account'],
        scope='selected' if plan['targets'] else 'all', usernames=plan['targets'],
        export_format=plan['format'], use_cache=True, output_dir=str(root),
        file_name=None, output_mode='folder', folder_name='archive')
    plan['export_id'] = job.export_id
    plan['output_dir'] = str(root)
    plan['last_refreshed_at'] = time.time()
    task['result'] = '朋友圈已刷新，正在增量保存内容与可获取的媒体'
    task['due'] = time.time() + 2


def cancel_export(plan):
    if plan.get('export_id'):
        manager().cancel_job(plan['export_id'])
