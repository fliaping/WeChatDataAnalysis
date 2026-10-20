"""Watch encrypted DB/WAL changes; serialize authenticated snapshot publication."""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from watchfiles import awatch
from .linux_support import local_sources, sync_weflow

FALLBACK_SECONDS = 2
TARGET_SECONDS = 3
logger = logging.getLogger(__name__)


def database_change(_change, path: str) -> bool:
    return path.endswith(('.db', '.db-wal'))


class SnapshotWatch:
    def __init__(self, config: Path):
        self.config = config
        self.state = {'mode': 'starting', 'target_seconds': TARGET_SECONDS,
                      'fallback_seconds': FALLBACK_SECONDS, 'last_duration_ms': None,
                      'failed_accounts': 0, 'last_error': None}

    def roots(self):
        return tuple(sorted(set(local_sources(self.config).values())))

    async def sync(self):
        started = time.monotonic()
        try:
            reports = await asyncio.to_thread(sync_weflow, self.config)
            self.state.update(last_duration_ms=round((time.monotonic() - started) * 1000),
                              failed_accounts=sum(bool(r['failed']) for r in reports),
                              last_error=None)
        except Exception as exc:
            # No keys, account names, paths or message bodies in diagnostics.
            self.state.update(last_error=type(exc).__name__,
                              last_duration_ms=round((time.monotonic() - started) * 1000))
            logger.warning('Linux snapshot refresh failed (%s)', type(exc).__name__)

    async def run(self):
        await self.sync()
        while True:
            try:
                roots = self.roots()
            except (OSError, ValueError) as exc:
                self.state.update(mode='polling_fallback', last_error=type(exc).__name__)
                await asyncio.sleep(FALLBACK_SECONDS)
                await self.sync()
                continue
            if not roots:
                self.state['mode'] = 'waiting_for_account'
                await asyncio.sleep(FALLBACK_SECONDS)
                await self.sync()
                continue
            try:
                self.state['mode'] = 'filesystem_events'
                # watchfiles uses inotify on Linux. Timeout scans recover from
                # missed events and detect new accounts or recreated roots.
                async for changes in awatch(*roots, watch_filter=database_change,
                        debounce=150, step=50, rust_timeout=FALLBACK_SECONDS * 1000,
                        yield_on_timeout=True, recursive=True, force_polling=False):
                    await self.sync()
                    if self.roots() != roots:
                        break
            except (OSError, RuntimeError) as exc:
                self.state.update(mode='polling_fallback', last_error=type(exc).__name__)
                await asyncio.sleep(FALLBACK_SECONDS)
                await self.sync()
