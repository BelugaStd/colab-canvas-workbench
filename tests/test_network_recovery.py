"""Network restoration must resume the original submitted task without redeployment."""
import asyncio
import sys
import time

import aiohttp
import pytest

from backend.colab import Colab
from backend.manager import Manager
from backend.store import Store
from tests.fake_colab import PROFILES, FakeColab, template
from tests.test_local_cli import launcher_at


async def eventually(condition, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        await asyncio.sleep(.03)
    raise AssertionError("Condition did not become true")


@pytest.fixture
async def recovery_rig(tmp_path):
    store, fake = Store(tmp_path), FakeColab()
    manager = Manager(store, fake, PROFILES)
    await manager.open()
    job = manager.submit('colab2', template(), 'Network recovery fixture', False)
    await eventually(lambda: manager.job(job['id'])['status'] == 'completed')
    await eventually(lambda: manager.workers['colab2'].done())
    manager.job(job['id'], status='recovering', outputs=[])
    manager.machine('colab2', phase='disconnected')
    try:
        yield manager, fake, job['id']
    finally:
        await manager.close()
        store.close()


@pytest.mark.parametrize('failure', ['lease', 'offline'])
async def test_failed_reconnect_keeps_retrying_original_prompt_after_network_returns(recovery_rig, monkeypatch, failure):
    manager, fake, key = recovery_rig
    account = 'colab2'
    session = manager.machine(account)['session']
    prompt_id = manager.job(key)['prompt_id']
    calls_before = [c for c in fake.calls if c[1] in {'create', 'exec'}]
    original = fake.tunnel
    attempts = []

    async def temporarily_unavailable(*args):
        attempts.append((time.monotonic(), args[1]))
        if len(attempts) == 1:
            if failure == 'lease':
                raise RuntimeError('Already-active SSH session (HTTP 429)')
            raise aiohttp.ClientConnectionError('Network is offline')
        return await original(*args)

    monkeypatch.setattr(fake, 'tunnel', temporarily_unavailable)
    manager.resume(key)
    await eventually(lambda: manager.job(key)['status'] == 'completed')
    assert len(attempts) >= 2
    assert attempts[1][0] - attempts[0][0] >= .5
    assert all(name == session for _, name in attempts)
    assert manager.machine(account)['session'] == session
    assert manager.job(key)['prompt_id'] == prompt_id
    assert len(fake.prompt_posts) == 1
    assert [c for c in fake.calls if c[1] in {'create', 'exec'}] == calls_before


async def test_cancelling_recovering_job_stops_connection_retries(recovery_rig, monkeypatch):
    manager, fake, key = recovery_rig
    attempts = []

    async def offline(*args):
        attempts.append(time.monotonic())
        raise aiohttp.ClientConnectionError('Network is offline')

    monkeypatch.setattr(fake, 'tunnel', offline)
    manager.resume(key)
    await eventually(lambda: bool(attempts))
    await manager.cancel(key)
    count = len(attempts)
    await eventually(lambda: manager.workers['colab2'].done(), timeout=8)
    await asyncio.sleep(.3)
    assert manager.job(key)['status'] == 'cancelled'
    assert len(attempts) == count
    assert len(fake.prompt_posts) == 1
    assert sum(c[1] == 'create' for c in fake.calls) == 1


async def test_repeated_network_flaps_do_not_strand_a_submitted_prompt(recovery_rig, monkeypatch):
    manager, fake, key = recovery_rig
    prompt_id = manager.job(key)['prompt_id']
    session = manager.machine('colab2')['session']
    original = manager.remote
    failures = 0

    async def unstable_history(account, method, path, *args, **kwargs):
        nonlocal failures
        if path.startswith('/history/') and failures < 3:
            failures += 1
            raise aiohttp.ClientConnectionError('Network switched while reading history')
        return await original(account, method, path, *args, **kwargs)

    monkeypatch.setattr(manager, 'remote', unstable_history)
    manager.resume(key)
    await eventually(lambda: manager.workers['colab2'].done(), timeout=35)
    assert manager.job(key)['status'] == 'completed'
    assert failures == 3
    assert manager.job(key)['prompt_id'] == prompt_id
    assert manager.machine('colab2')['session'] == session
    assert len(fake.prompt_posts) == 1
    assert sum(c[1] == 'create' for c in fake.calls) == 1
    assert sum(c[1] == 'exec' for c in fake.calls) == 1


async def test_tunnel_retries_new_ssh_session_limit_message_on_same_runtime(tmp_path, monkeypatch):
    from backend import colab

    launcher = launcher_at(tmp_path, 'pass')
    identity = tmp_path / 'key'
    identity.touch()
    create = asyncio.create_subprocess_exec
    attempts = []

    async def lease_then_listen(*args, **kwargs):
        if args[0] != 'ssh':
            return await create(*args, **kwargs)
        attempts.append(args)
        if len(attempts) == 1:
            code = "import sys;print('SSH session limit reached (HTTP 429): 1 of 1 SSH sessions in use on this runtime.',file=sys.stderr);sys.exit(1)"
        else:
            forwarding = args[args.index('-L') + 1]
            port = int(forwarding.split(':')[1])
            code = f"import socket,time;s=socket.socket();s.bind(('127.0.0.1',{port}));s.listen();time.sleep(60)"
        return await create(sys.executable, '-c', code, **kwargs)

    monkeypatch.setattr(colab.asyncio, 'create_subprocess_exec', lease_then_listen)
    cli = Colab([{'id': 'colab1', 'launcher': str(launcher)}], tmp_path)
    try:
        port = await cli.tunnel('colab1', 'original-runtime', str(identity))
        assert len(attempts) == 2
        assert all('original-runtime' in ' '.join(args) for args in attempts)
        _, writer = await asyncio.open_connection('127.0.0.1', port)
        writer.close()
        await writer.wait_closed()
    finally:
        await cli.close()
