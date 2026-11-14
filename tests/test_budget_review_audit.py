import json

import pytest

from backend import agent_review, production
from tests import test_agent_tools
from tests.test_agent_tools import asset
from tests.test_production import plan

ctx = test_agent_tools.ctx


def budget_account(ctx, balance):
    ctx.manager.store.put('accounts', ctx.account, {'usage': {'balance': balance}})


def authorize(ctx):
    ctx.manager.store.put('production_budgets', ctx.account,
        {'authorization_id': 'original-user-budget', 'balance_before': 700, 'limit_cu': 100})


def test_cu_usage_is_monotonic_across_balance_credit_and_restart(ctx):
    authorize(ctx)
    budget_account(ctx, 640)
    assert production.budget(ctx.manager.store, ctx.account)['gross_used_cu'] == 60
    budget_account(ctx, 740)
    assert production.budget(ctx.manager.store, ctx.account)['remaining_cu'] == 40
    budget_account(ctx, 710)
    assert production.budget(ctx.manager.store, ctx.account)['gross_used_cu'] == 90
    assert production.budget(ctx.manager.store, ctx.account)['gross_used_cu'] == 90
    budget_account(ctx, 704)
    with pytest.raises(ValueError, match='共享预算'):
        production.guard_budget(ctx.manager.store, ctx.account)
    assert production.budget(ctx.manager.store, ctx.account)['gross_used_cu'] == 96


@pytest.mark.parametrize('balance', [float('nan'), float('inf'), -float('inf'), True, None])
def test_budget_never_accepts_unknown_or_nonfinite_balance(ctx, balance):
    authorize(ctx)
    budget_account(ctx, balance)
    with pytest.raises(ValueError):
        production.guard_budget(ctx.manager.store, ctx.account)


@pytest.mark.asyncio
@pytest.mark.parametrize('output', ['not JSON', json.dumps({'verdict': 'pass', 'checks': []})])
async def test_malformed_independent_judge_output_stops_gpu_expansion(ctx, monkeypatch, output):
    project = ctx.manager.store.get('projects', ctx.project_id)
    production.save_plan(project['canvases'][0], plan(False))
    ctx.manager.store.put('projects', ctx.project_id, project)
    key = asset(ctx)

    async def complete(*args, **kwargs):
        return {'text': output, 'usage': {}}

    async def inspect(args):
        return await agent_review.review(ctx, args, None, {}, lambda usage: None)

    monkeypatch.setattr(agent_review, 'complete', complete)
    ctx.review = inspect
    with pytest.raises(agent_review.ReviewUnavailable):
        await agent_review.invoke(ctx, {'asset_id': key, 'purpose': 'master', 'entity_id': 'hero'})
    assert ctx.manager.store.all('production_reviews') == []


@pytest.mark.asyncio
async def test_bad_review_prevents_later_generation_in_the_same_model_tool_batch(tmp_path, monkeypatch):
    from backend.agent_tools import ToolContext
    from tests.test_agent_service import setup_client, turn, wait_run
    source = None

    async def director(*args, **kwargs):
        return turn(calls=[
            {'id': 'review', 'name': 'review_media', 'arguments': json.dumps({'asset_id': source, 'purpose': 'master', 'entity_id': 'hero'})},
            {'id': 'paid-next', 'name': 'generate_nodes', 'arguments': json.dumps({'node_ids': ['new-image'], 'expected_revision': 0})},
        ])

    async def malformed_judge(*args, **kwargs):
        return {'text': 'not JSON', 'usage': {}}

    monkeypatch.setattr(agent_review, 'complete', malformed_judge)
    app, client, lifecycle, project, session = await setup_client(tmp_path, monkeypatch, director)
    try:
        context = ToolContext(app.state.manager, project['id'], project['canvases'][0]['id'], 'colab1', 'run', 'review')
        source = asset(context)
        production.save_plan(project['canvases'][0], plan(False))
        app.state.store.put('projects', project['id'], project)
        response = await client.post(f"/api/agent/sessions/{session['id']}/messages", json={
            'message': '审查后继续生成', 'mode': 'auto', 'account': 'colab1', 'request_id': 'judge-failfast'})
        run = await wait_run(client, response.json()['id'], expected='failed')
        assert '质检' in run['error']
        assert [item['name'] for item in app.state.store.all('agent_calls')] == ['review_media']
        assert not app.state.store.all('jobs')
    finally:
        await client.aclose()
        await lifecycle.__aexit__(None, None, None)
