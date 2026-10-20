"""Notify clients only after a Linux snapshot synchronization is published."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from fastapi import Request
from fastapi.responses import StreamingResponse


def snapshot_state(account_dir: Path) -> dict:
    try:
        state = json.loads((account_dir / '_linux_sync.json').read_text())
        return {key: state.get(key, 0) for key in
                ('revision', 'last_success_at', 'last_attempt_at', 'failed_count')}
    except (OSError, ValueError, AttributeError):
        return {}


def stream_linux_snapshot_events(request: Request, account_dir: Path) -> StreamingResponse:
    async def events():
        previous_revision = None
        last_heartbeat = time.monotonic()
        ready = True
        while not await request.is_disconnected():
            state = await asyncio.to_thread(snapshot_state, account_dir)
            revision = state.get('revision', 0)
            if ready or revision != previous_revision:
                payload = {'type': 'ready' if ready else 'snapshot_updated',
                           'mode': 'linux_snapshot', 'account': account_dir.name,
                           'refreshIntervalSeconds': 30, **state}
                yield 'data: ' + json.dumps(payload, separators=(',', ':')) + '\n\n'
                previous_revision, ready = revision, False
            if time.monotonic() - last_heartbeat >= 15:
                yield ': ping\n\n'
                last_heartbeat = time.monotonic()
            await asyncio.sleep(1)

    return StreamingResponse(events(), media_type='text/event-stream', headers={
        'Cache-Control': 'no-cache', 'Connection': 'keep-alive', 'X-Accel-Buffering': 'no'})
