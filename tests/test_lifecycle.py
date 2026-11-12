import asyncio
import time

import pytest

from backend.manager import Manager
from backend.store import Store
from tests.fake_colab import PROFILES, FakeColab, template


async def eventually(condition, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        await asyncio.sleep(0.03)
    raise AssertionError("Condition did not become true")


@pytest.fixture
async def rig(tmp_path):
    store, fake = Store(tmp_path), FakeColab()
    manager = Manager(store, fake, PROFILES)
    await manager.open()
    yield manager, fake, store
    await manager.close()
    store.close()


async def test_generate_download_then_release_owned_machine_only(rig):
    manager, fake, store = rig
    job = manager.submit("colab2", template(), "Fox", True)
    await eventually(lambda: manager.job(job['id'])['status'] == 'completed')
    await eventually(lambda: manager.machine('colab2')['phase'] == 'stopped')
    result = manager.job(job['id'])
    output = store.directory / 'outputs' / job['id'] / result['outputs'][0]['filename']
    assert output.stat().st_size > 100_000
    assert (output.parent / 'workflow-api.json').is_file()
    assert fake.active['colab1'][0]['name'] == 'unrelated-important-session'
    assert len(fake.prompt_posts) == 1
    assert fake.downloaded
    assert all(call[0] == 'colab2' for call in fake.calls)


async def test_start_is_idempotent_and_accounts_are_independent(rig):
    manager, fake, _ = rig
    manager.request_start('colab2')
    manager.request_start('colab2')
    manager.request_start('colab3')
    await eventually(lambda: all(manager.machine(a)['phase'] == 'ready' for a in ('colab2', 'colab3')))
    assert len([c for c in fake.calls if c[1] == 'create']) == 2
    manager.request_stop('colab2')
    await eventually(lambda: manager.machine('colab2')['phase'] == 'stopped')
    assert manager.machine('colab3')['phase'] == 'ready'
    assert len(fake.active['colab3']) == 1


async def test_stop_during_allocation_waits_and_releases(rig):
    manager, fake, _ = rig
    fake.create_gate = asyncio.Event()
    manager.request_start('colab2')
    await eventually(lambda: any(c[1] == 'create' for c in fake.calls))
    manager.request_stop('colab2')
    fake.create_gate.set()
    await eventually(lambda: manager.machine('colab2')['phase'] == 'stopped')
    assert not fake.active['colab2']
    assert not any(c[1] == 'exec' for c in fake.calls)


async def test_refuses_unowned_session_and_reports_failed_shutdown(rig):
    manager, fake, _ = rig
    manager.machine('colab1', session='unrelated-important-session', phase='ready')
    manager.request_stop('colab1')
    await eventually(lambda: manager.machine('colab1')['phase'] == 'error')
    assert not any(c[1] == 'stop' for c in fake.calls)
    manager.machine('colab1', session=None, phase='stopped')
    manager.request_start('colab2')
    await eventually(lambda: manager.machine('colab2')['phase'] == 'ready')
    fake.fail_stop = True
    manager.request_stop('colab2')
    await eventually(lambda: manager.machine('colab2')['phase'] == 'error')
    assert manager.machine('colab2')['session']
    assert len(fake.active['colab2']) == 1
    fake.fail_stop = False


async def test_cancel_targets_prompt_and_stop_rejects_active_queue(rig):
    manager, fake, _ = rig
    fake.delay = 30
    job = manager.submit('colab2', template(), 'Cancel', False)
    await eventually(lambda: bool(fake.prompt_posts))
    with pytest.raises(ValueError, match='未完成任务'):
        manager.request_stop('colab2')
    await manager.cancel(job['id'])
    assert manager.job(job['id'])['status'] == 'cancelled'
    assert ('colab2', 'cancel', manager.job(job['id'])['prompt_id']) in fake.calls
    manager.request_stop('colab2')
    await eventually(lambda: manager.machine('colab2')['phase'] == 'stopped')


async def test_recovery_reads_history_without_resubmitting(rig):
    manager, fake, store = rig
    job = manager.submit('colab2', template(), 'Recover', False)
    await eventually(lambda: manager.job(job['id'])['status'] == 'completed')
    posts = len(fake.prompt_posts)
    manager.job(job['id'], status='recovering', outputs=[])
    manager.resume(job['id'])
    await eventually(lambda: manager.job(job['id'])['status'] == 'completed')
    assert len(fake.prompt_posts) == posts


async def test_reconnect_live_runtime_recovers_output_without_bootstrap(rig):
    manager, fake, _ = rig
    job = manager.submit('colab2', template(), 'Reconnect', False)
    await eventually(lambda: manager.job(job['id'])['status'] == 'completed')
    session = manager.machine('colab2')['session']
    calls = list(fake.calls)
    posts = len(fake.prompt_posts)
    manager.job(job['id'], status='recovering', outputs=[])
    manager.machine('colab2', phase='disconnected', port=None)
    await manager.start('colab2')
    await eventually(lambda: manager.job(job['id'])['status'] == 'completed')
    assert manager.machine('colab2')['session'] == session
    assert len(fake.prompt_posts) == posts
    assert [c for c in fake.calls if c[1] in {'exec', 'create'}] == [c for c in calls if c[1] in {'exec', 'create'}]


async def test_submitted_job_reconnects_after_transport_failure(rig, monkeypatch):
    import aiohttp

    manager, fake, _ = rig
    remote = manager.remote
    dropped = False
    async def disconnect_once(account, method, path, *args, **kwargs):
        nonlocal dropped
        if path.startswith('/history/') and not dropped:
            dropped = True
            raise aiohttp.ClientConnectionError('test SSH connection lost')
        return await remote(account, method, path, *args, **kwargs)
    monkeypatch.setattr(manager, 'remote', disconnect_once)
    job = manager.submit('colab2', template(), 'Transient connection loss', False)
    await eventually(lambda: manager.job(job['id'])['status'] == 'completed', timeout=6)
    assert dropped
    assert len(fake.prompt_posts) == 1
    assert len([c for c in fake.calls if c[1] == 'create']) == 1
    assert len([c for c in fake.calls if c[1] == 'exec']) == 1


async def test_automatic_recovery_never_allocates_a_replacement_runtime(rig):
    manager, fake, store = rig
    store.put('jobs', 'gone', {'id': 'gone', 'account': 'colab2', 'status': 'recovering',
                             'prompt_id': 'original-prompt', 'created_at': time.time()})
    manager.machine('colab2', phase='disconnected', session='wb-original-missing')
    manager.resume('gone')
    await eventually(lambda: manager.workers['colab2'].done())
    assert manager.job('gone')['status'] == 'failed'
    assert not any(c[1] in {'create', 'exec'} for c in fake.calls)
    assert not fake.prompt_posts


async def test_tunnel_failure_does_not_trigger_redeployment(rig, monkeypatch):
    manager, fake, _ = rig
    await manager.start('colab2')
    calls = list(fake.calls)
    async def failed_tunnel(*args):
        raise RuntimeError('Already-active SSH session (HTTP 429)')
    monkeypatch.setattr(fake, 'tunnel', failed_tunnel)
    manager.machine('colab2', phase='disconnected', port=None)
    await manager.start('colab2')
    assert manager.machine('colab2')['phase'] == 'error'
    assert '429' in manager.machine('colab2')['message']
    assert [c for c in fake.calls if c[1] in {'create', 'exec'}] == [c for c in calls if c[1] in {'create', 'exec'}]


async def test_unknown_old_session_does_not_rerun_submitted_job(rig):
    manager, fake, store = rig
    store.put('jobs', 'lost', {'id': 'lost', 'account': 'colab2', 'status': 'recovering', 'prompt_id': 'old-prompt', 'created_at': time.time()})
    manager.machine('colab2', phase='disconnected', session='wb-old-missing')
    manager.request_start('colab2')
    await eventually(lambda: manager.machine('colab2')['phase'] == 'ready')
    assert manager.job('lost')['status'] == 'failed'
    assert not fake.prompt_posts


async def test_stop_after_job_prevents_adding_work_behind_it(rig):
    manager, fake, _ = rig
    fake.delay = 30
    manager.submit('colab2', template(), 'Final', True)
    with pytest.raises(ValueError, match='完成后关机'):
        manager.submit('colab2', template(), 'Too late', False)


async def test_shutdown_during_worker_allocation_does_not_orphan_vm(tmp_path):
    store, fake = Store(tmp_path), FakeColab()
    manager = Manager(store, fake, PROFILES)
    fake.create_gate = asyncio.Event()
    await manager.open()
    manager.submit('colab2', template(), 'In flight', False)
    await eventually(lambda: any(c[1] == 'create' for c in fake.calls))
    closing = asyncio.create_task(manager.close())
    await asyncio.sleep(0.1)
    fake.create_gate.set()
    await asyncio.wait_for(closing, timeout=5)
    assert not fake.active['colab2']
    assert any(c[1] == 'stop' for c in fake.calls)
    store.close()


async def test_manual_stop_detects_native_editor_tasks(rig):
    manager, fake, _ = rig
    manager.request_start('colab2')
    await eventually(lambda: manager.machine('colab2')['phase'] == 'ready')
    fake.native_busy = True
    manager.request_stop('colab2')
    await eventually(lambda: '仍有任务' in manager.machine('colab2')['message'])
    assert manager.machine('colab2')['phase'] == 'ready'
    assert not any(c[1] == 'stop' for c in fake.calls)
    assert '仍有任务' in manager.machine('colab2')['message']
    fake.native_busy = False


async def test_idle_shutdown_waits_for_native_queue_then_stops(rig):
    manager, fake, _ = rig
    manager.request_start('colab2')
    await eventually(lambda: manager.machine('colab2')['phase'] == 'ready')
    manager.machine('colab2', last_activity=time.time() - 3600)
    fake.native_busy = True
    await manager.idle_sweep()
    assert manager.machine('colab2')['phase'] == 'ready'
    assert not any(c[1] == 'stop' for c in fake.calls)
    fake.native_busy = False
    manager.machine('colab2', last_activity=time.time() - 3600)
    await manager.idle_sweep()
    await eventually(lambda: manager.machine('colab2')['phase'] == 'stopped')


async def test_same_gpu_jobs_are_serial_and_different_accounts_run_in_parallel(rig):
    manager, fake, _ = rig
    fake.delay = 30
    a = manager.submit('colab2', template(), 'A', False)
    b = manager.submit('colab2', template(), 'B', False)
    c = manager.submit('colab3', template(), 'C', False)
    await eventually(lambda: len(fake.prompt_posts) == 2)
    assert {account for account, _ in fake.prompt_posts} == {'colab2', 'colab3'}
    assert manager.job(b['id'])['status'] == 'pending'
    await manager.cancel(a['id'])
    await eventually(lambda: len(fake.prompt_posts) == 3)
    assert manager.job(c['id'])['status'] in {'queued', 'running'}
    assert fake.prompt_posts[-1][0] == 'colab2'
    await manager.cancel(b['id'])
    await manager.cancel(c['id'])


async def test_connect_all_waits_for_full_model_preparation(rig, monkeypatch):
    manager, _, _ = rig
    seen = []
    async def prepare(job=None, **kwargs):
        seen.append(kwargs['models'])
    monkeypatch.setattr(manager, 'prepare_models', prepare)
    await manager.start('colab2', full=True)
    assert manager.machine('colab2')['phase'] == 'ready'
    assert set(seen[-1]) == {'minimax-h3', 'minimax-h3-reference', 'minimax-image', 'moss-image', 'moss-edit', 'upscale',
                             'seedvr2-7b', 'seedvr2-7b-sharp', 'seedvr2-3b'}


async def test_stale_usage_refresh_cannot_forget_a_newly_allocated_session(rig, monkeypatch):
    manager, fake, _ = rig
    entered, release = asyncio.Event(), asyncio.Event()
    async def delayed_sessions(account):
        entered.set()
        await release.wait()
        return []
    monkeypatch.setattr(fake, 'sessions', delayed_sessions)
    refreshing = asyncio.create_task(manager.refresh('colab2'))
    await entered.wait()
    manager.machine('colab2', phase='ready', session='new-owned-session')
    release.set()
    await refreshing
    assert manager.machine('colab2')['session'] == 'new-owned-session'
    manager.machine('colab2', phase='deploying')
    await manager.refresh('colab2')
    assert manager.machine('colab2')['session'] == 'new-owned-session'
    manager.machine('colab2', phase='stopped', session=None)
