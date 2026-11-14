import asyncio
import time

import pytest

from backend import production
from tests import test_agent_tools

ctx = test_agent_tools.ctx


def authorize(ctx, *, used=50, age=0, rate=20):
    store = ctx.manager.store
    now = time.time()
    store.put('production_budgets', ctx.account,
        {'authorization_id': 'original', 'balance_before': 700, 'limit_cu': 100})
    store.put('accounts', ctx.account, {'usage': {'balance': 700 - used, 'rate': rate},
        'checked_at': now - age, 'usage_checked_at': now - age, 'error': None})
    session = f"wb-{store.get('meta', 'install')['id']}-{ctx.account[-1]}-budget"
    ctx.manager.machine(ctx.account, phase='disconnected', session=session)
    ctx.manager.colab.active[ctx.account] = [{'name': session}]
    return session


def test_stale_quota_is_visible_and_blocks_new_spend_without_overwriting_actual(ctx):
    authorize(ctx, used=50, age=120, rate=30)
    value = production.budget(ctx.manager.store, ctx.account)
    assert value['stale'] and value['quota_status'] == 'unknown'
    assert value['gross_used_cu'] == 50
    assert value['estimated_used_cu'] >= 51
    assert value['estimated_remaining_cu'] <= 49
    with pytest.raises(ValueError, match='额度|陈旧|未知'):
        production.guard_budget(ctx.manager.store, ctx.account)
    stored = ctx.manager.store.get('production_budgets', ctx.account)
    assert stored['gross_used_cu'] == 50


@pytest.mark.asyncio
async def test_reserve_floor_forces_owned_shutdown_despite_active_jobs_and_speech(ctx, monkeypatch):
    session = authorize(ctx, used=96)
    store, manager = ctx.manager.store, ctx.manager
    store.put('jobs', 'busy', {'id': 'busy', 'account': ctx.account, 'status': 'running', 'created_at': 1})
    store.put('agent_speech_tasks', 'speech', {'id': 'speech', 'account': ctx.account, 'session': session, 'status': 'running'})

    async def usage(_):
        return {'balance': 604, 'rate': 20 if manager.machine(ctx.account)['session'] else 0}

    monkeypatch.setattr(manager.colab, 'usage', usage)
    await manager.budget_sweep()
    await manager.stop_tasks[ctx.account]
    assert manager.machine(ctx.account)['session'] is None
    assert ('colab2', 'stop', session) in manager.colab.calls
    assert store.get('jobs', 'busy')['status'] == 'cancelled'
    ledger = store.get('production_budgets', ctx.account)
    assert ledger['shutdown_status'] == 'confirmed'
    assert production.budget(store, ctx.account)['gross_used_cu'] == 96
    assert any('预算' in item['message'] for item in store.get('logs', ctx.account))


@pytest.mark.asyncio
async def test_continuous_quota_failure_stops_owned_gpu_without_claiming_fresh_data(ctx, monkeypatch):
    now = [time.time()]
    monkeypatch.setattr(production.time, 'time', lambda: now[0])
    session = authorize(ctx, used=50, age=60)
    manager, store = ctx.manager, ctx.manager.store

    async def failure(_):
        raise RuntimeError('quota endpoint disconnected')

    monkeypatch.setattr(manager.colab, 'usage', failure)
    await manager.budget_sweep()
    assert ctx.account not in manager.stop_tasks
    value = production.budget(store, ctx.account)
    assert value['quota_status'] == 'unknown' and value['gross_used_cu'] == 50
    assert 'quota endpoint disconnected' in value['quota_error']
    now[0] += 61
    await manager.budget_sweep()
    await manager.stop_tasks[ctx.account]
    assert ('colab2', 'stop', session) in manager.colab.calls
    assert manager.machine(ctx.account)['session'] is None
    assert store.get('production_budgets', ctx.account)['shutdown_status'] == 'confirmed'


@pytest.mark.asyncio
async def test_quota_timeout_is_bounded_and_its_failure_is_visible(ctx, monkeypatch):
    import backend.manager as manager_module
    authorize(ctx, used=20)
    monkeypatch.setattr(manager_module, 'BUDGET_REFRESH_TIMEOUT_SECONDS', .01)

    async def hanging(_):
        await asyncio.Event().wait()

    monkeypatch.setattr(ctx.manager.colab, 'usage', hanging)
    await asyncio.wait_for(ctx.manager.budget_sweep(), .5)
    assert production.budget(ctx.manager.store, ctx.account)['quota_status'] == 'unknown'
    assert ctx.account not in ctx.manager.stop_tasks


@pytest.mark.asyncio
async def test_failed_budget_shutdown_stays_unknown_and_retries(ctx, monkeypatch):
    now = [time.time()]
    monkeypatch.setattr(production.time, 'time', lambda: now[0])
    session = authorize(ctx, used=96)
    manager, store = ctx.manager, ctx.manager.store

    async def usage(_):
        return {'balance': 604, 'rate': 20}

    monkeypatch.setattr(manager.colab, 'usage', usage)
    manager.colab.fail_stop = True
    await manager.budget_sweep()
    await manager.stop_tasks[ctx.account]
    assert manager.machine(ctx.account)['session'] == session
    assert store.get('production_budgets', ctx.account)['shutdown_status'] == 'unknown'
    assert any('关机失败' in item['message'] for item in store.get('logs', ctx.account))
    manager.colab.fail_stop = False
    now[0] += 31
    await manager.budget_sweep()
    await manager.stop_tasks[ctx.account]
    assert manager.machine(ctx.account)['session'] is None
    assert store.get('production_budgets', ctx.account)['shutdown_status'] == 'confirmed'


@pytest.mark.asyncio
async def test_estimated_reserve_floor_stops_before_outage_deadline(ctx, monkeypatch):
    session = authorize(ctx, used=94, age=45, rate=120)

    async def failure(_):
        raise RuntimeError('quota disconnected')

    monkeypatch.setattr(ctx.manager.colab, 'usage', failure)
    await ctx.manager.budget_sweep()
    await ctx.manager.stop_tasks[ctx.account]
    assert (ctx.account, 'stop', session) in ctx.manager.colab.calls
    value = production.budget(ctx.manager.store, ctx.account)
    assert value['gross_used_cu'] == 94
    assert value['estimated_remaining_cu'] <= 5


@pytest.mark.asyncio
async def test_invalid_quota_responses_cannot_reset_outage_timer(ctx, monkeypatch):
    now = [time.time()]
    monkeypatch.setattr(production.time, 'time', lambda: now[0])
    authorize(ctx, used=50, age=60)

    async def invalid(_):
        return {'balance': None, 'rate': 20}

    monkeypatch.setattr(ctx.manager.colab, 'usage', invalid)
    await ctx.manager.budget_sweep()
    assert ctx.account not in ctx.manager.stop_tasks
    now[0] += 61
    await ctx.manager.budget_sweep()
    await ctx.manager.stop_tasks[ctx.account]
    assert ctx.manager.machine(ctx.account)['session'] is None
    assert production.budget(ctx.manager.store, ctx.account)['gross_used_cu'] == 50


@pytest.mark.asyncio
async def test_budget_shutdown_cancels_stalled_deployment_and_releases_account_lock(ctx, monkeypatch):
    session = authorize(ctx, used=96)
    manager = ctx.manager
    entered = asyncio.Event()

    async def deploy_forever():
        async with manager.locks[ctx.account]:
            manager.machine(ctx.account, phase='deploying')
            entered.set()
            await asyncio.Event().wait()

    async def usage(_):
        return {'balance': 604, 'rate': 20}

    task = asyncio.create_task(deploy_forever())
    manager.start_tasks[ctx.account] = task
    await entered.wait()
    monkeypatch.setattr(manager.colab, 'usage', usage)
    await manager.budget_sweep()
    await asyncio.wait_for(manager.stop_tasks[ctx.account], .5)
    assert task.cancelled()
    assert manager.machine(ctx.account)['session'] is None
    assert (ctx.account, 'stop', session) in manager.colab.calls
    with pytest.raises(ValueError, match='预算'):
        manager.request_start(ctx.account)


@pytest.mark.asyncio
async def test_background_maintenance_checks_budget_and_survives_guard_failure(ctx, monkeypatch):
    import backend.manager as manager_module
    authorize(ctx)
    calls = []

    async def tick(_):
        if calls:
            raise asyncio.CancelledError

    async def budget_failure():
        calls.append('budget')
        raise RuntimeError('observable maintenance failure')

    async def idle():
        calls.append('idle')

    monkeypatch.setattr(manager_module.asyncio, 'sleep', tick)
    monkeypatch.setattr(ctx.manager, 'budget_sweep', budget_failure)
    monkeypatch.setattr(ctx.manager, 'idle_sweep', idle)
    with pytest.raises(asyncio.CancelledError):
        await ctx.manager.maintain()
    assert calls == ['budget', 'idle']
    assert any('预算维护失败' in item['message'] for item in ctx.manager.store.get('logs', ctx.account))


@pytest.mark.asyncio
async def test_budget_maintenance_never_starts_gpu_or_stops_unowned_or_unbudgeted_session(ctx):
    manager = ctx.manager
    manager.machine(ctx.account, phase='ready', session='unowned-session')
    await manager.budget_sweep()
    assert manager.colab.calls == []
    authorize(ctx, used=96)
    manager.machine(ctx.account, phase='ready', session='unowned-session')
    await manager.budget_sweep()
    assert not any(call[1] in {'create', 'stop'} for call in manager.colab.calls)
    assert manager.machine(ctx.account)['session'] == 'unowned-session'


def test_fresh_usage_reconciles_actual_without_charging_the_estimate_twice(ctx):
    authorize(ctx, used=50, age=120, rate=30)
    assert production.budget(ctx.manager.store, ctx.account)['estimated_used_cu'] >= 51
    ctx.manager.store.put('accounts', ctx.account, {'usage': {'balance': 649, 'rate': 30},
        'checked_at': time.time(), 'usage_checked_at': time.time(), 'error': None})
    value = production.budget(ctx.manager.store, ctx.account)
    assert value['gross_used_cu'] == 51
    assert value['estimated_used_cu'] == pytest.approx(51, abs=.001)
    assert not value['stale']
