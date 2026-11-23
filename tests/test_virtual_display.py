import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.parser.errors import BrowserError
from app.virtual_display import VirtualDisplay


@pytest.fixture
def xvfb(monkeypatch):
    process = SimpleNamespace(returncode=None, terminate=lambda: None, wait=AsyncMock(), kill=lambda: None)
    launched = []

    async def spawn(*args, **kwargs):
        launched.append(args)
        os.write(kwargs['pass_fds'][0], b'77\n')
        return process

    writer = SimpleNamespace(close=lambda: None, wait_closed=AsyncMock())
    connect = AsyncMock(return_value=(None, writer))
    monkeypatch.setattr('app.virtual_display.asyncio.create_subprocess_exec', spawn)
    monkeypatch.setattr('app.virtual_display.asyncio.open_unix_connection', connect)
    return SimpleNamespace(process=process, launched=launched, connect=connect)


async def test_display_waits_for_socket_and_disables_reset(xvfb):
    display = VirtualDisplay()
    try:
        assert await display.start() == ':77'
        assert '-noreset' in xvfb.launched[0]
        xvfb.connect.assert_awaited_once_with('/tmp/.X11-unix/X77')
        assert display.stderr is not None
    finally:
        await display.close()
    assert display.process is None and display.stderr is None
    xvfb.process.wait.assert_awaited_once()


async def test_unavailable_display_socket_releases_process(xvfb):
    xvfb.connect.side_effect = ConnectionRefusedError()
    display = VirtualDisplay()
    with pytest.raises(BrowserError, match='display is unavailable'):
        await display.start()
    assert display.process is None and display.stderr is None
    xvfb.process.wait.assert_awaited_once()


async def test_cancelled_display_start_releases_process(xvfb):
    xvfb.connect.side_effect = asyncio.CancelledError()
    display = VirtualDisplay()
    with pytest.raises(asyncio.CancelledError):
        await display.start()
    assert display.process is None and display.stderr is None
