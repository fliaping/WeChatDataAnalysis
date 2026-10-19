"""Owner-only Linux integration; the API must stay behind the authenticated proxy."""
import os
import sys
import asyncio
import shutil
from pathlib import Path
from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from ..linux_support import sync_weflow, available_accounts, local_sources, capture_linux_key

router = APIRouter(prefix='/api/linux', tags=['Linux'])
_capture_job = None
_capture_result = {'status': 'idle'}

def _config():
    return Path(os.environ.get('WECHAT_TOOL_WEFLOW_CONFIG', str(Path.home() / '.config/weflow/WeFlow-config.json')))

@router.get('/status')
def status():
    try:
        accounts = [{'account': x['account'], 'database_key_available': bool(x['key']),
                     'image_key_available': bool(x.get('aes'))} for x in available_accounts(_config())]
    except (OSError, ValueError):
        accounts = []
    return {'platform': sys.platform, 'supported': sys.platform.startswith('linux'),
            'accounts': accounts, 'image_decryption': ['XOR', 'V2 AES + XOR'],
            'memory_capture': sys.platform.startswith('linux'),
            'capture_available': bool(shutil.which('gdb')),
            'local_accounts': list(local_sources(_config())), 'capture': _capture_result}

def _owner(request: Request):
    if request.headers.get('X-WeHarbor-Action') != 'sync':
        raise HTTPException(403, 'X-WeHarbor-Action: sync is required')
    origin = request.headers.get('origin')
    from urllib.parse import urlsplit
    if origin and urlsplit(origin).netloc != request.headers.get('host', ''):
        raise HTTPException(403, 'Cross-origin requests are not allowed')

@router.post('/capture')
async def capture(request: Request):
    global _capture_job, _capture_result
    _owner(request)
    if _capture_job and not _capture_job.done():
        raise HTTPException(409, 'A capture is already running')
    account = (await request.json()).get('account')
    if account not in local_sources(_config()):
        raise HTTPException(400, 'Select a local account')
    async def run():
        global _capture_result
        try:
            result = await run_in_threadpool(capture_linux_key, account, _config())
            _capture_result = {'status': 'complete', 'result': result}
        except Exception as exc:
            _capture_result = {'status': 'failed', 'error': type(exc).__name__,
                'detail': 'Capture failed; check ptrace permission and complete a manual WeChat login before the timeout.'}
    _capture_result = {'status': 'waiting_for_manual_login', 'account': account}
    _capture_job = asyncio.create_task(run())
    return _capture_result

@router.post('/sync')
async def sync(request: Request):
    _owner(request)
    try:
        return {'accounts': await run_in_threadpool(sync_weflow, _config())}
    except (OSError, ValueError):
        raise HTTPException(409, 'No usable local WeFlow account; configure the source and key locally')
