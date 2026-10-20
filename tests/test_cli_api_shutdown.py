"""Local CLI drains have a finite backstop without an optional Gunicorn import."""
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

import pytest


@pytest.mark.parametrize('raw,expected', [(None, 20), ('1', 1), ('', 20), ('0', 20), ('-1', 20), ('bad', 20), ('2.5', 20)])
def test_cli_uses_existing_positive_shutdown_contract(monkeypatch, raw, expected):
    from novelvideo import cli
    import uvicorn
    if raw is None:
        monkeypatch.delenv('NOVELVIDEO_API_GRACEFUL_SHUTDOWN_TIMEOUT', raising=False)
    else:
        monkeypatch.setenv('NOVELVIDEO_API_GRACEFUL_SHUTDOWN_TIMEOUT', raw)
    monkeypatch.setitem(sys.modules, 'gunicorn', None)
    configs = []
    monkeypatch.setattr(uvicorn, 'run', lambda app, **kwargs: configs.append(kwargs))
    cli.api(port=9876, host='127.0.0.1', reload=False)
    assert configs[0].get('timeout_graceful_shutdown') == expected


def test_cli_sigterm_exits_with_blocked_request_and_coordinator_cleanup(tmp_path):
    driver = tmp_path / 'driver.py'
    driver.write_text('''
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import sys
import uvicorn
from fastapi import FastAPI
from novelvideo import cli
root = Path(sys.argv[1])
@asynccontextmanager
async def lifespan(app):
    from novelvideo.chat.reference_remix_continuation import ReferenceRemixCoordinator
    coordinator = ReferenceRemixCoordinator()
    async def worker():
        try:
            await asyncio.Event().wait()
        finally:
            (root / 'worker-cleaned').touch()
    coordinator._workers['local-test'] = asyncio.create_task(worker())
    yield
    await coordinator.shutdown()
    (root / 'application-cleaned').touch()
app = FastAPI(lifespan=lifespan)
@app.get('/blocked')
async def blocked():
    (root / 'request-started').touch()
    try:
        await asyncio.Event().wait()
    finally:
        (root / 'request-cleaned').touch()
real_run = uvicorn.run
uvicorn.run = lambda target, **kwargs: real_run(app, **kwargs)
cli.api(port=int(sys.argv[2]), host='127.0.0.1', reload=False)
''')
    with socket.socket() as reserved:
        reserved.bind(('127.0.0.1', 0))
        port = reserved.getsockname()[1]
    env = {**os.environ, 'NOVELVIDEO_API_GRACEFUL_SHUTDOWN_TIMEOUT': '1'}
    process = subprocess.Popen([sys.executable, str(driver), str(tmp_path), str(port)], env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    client = None
    try:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            try:
                client = socket.create_connection(('127.0.0.1', port), timeout=0.1)
                break
            except OSError:
                assert process.poll() is None
                time.sleep(0.02)
        assert client is not None
        client.sendall(b'GET /blocked HTTP/1.1\r\nHost: localhost\r\n\r\n')
        while not (tmp_path / 'request-started').exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert (tmp_path / 'request-started').exists()
        started = time.monotonic()
        process.send_signal(signal.SIGTERM)
        output, _ = process.communicate(timeout=5)
        assert time.monotonic() - started < 5, output
        assert process.returncode in (0, -signal.SIGTERM), output
        assert all((tmp_path / name).exists() for name in ('request-cleaned', 'worker-cleaned', 'application-cleaned')), output
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
        if client is not None:
            client.close()
