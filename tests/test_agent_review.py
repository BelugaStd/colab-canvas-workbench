import json

import pytest

from backend import agent_review
from backend.agent_provider import ProviderError
from tests import test_agent_tools
from tests.test_agent_tools import asset
from tests.test_production import plan

ctx = test_agent_tools.ctx


async def test_independent_review_always_requests_stream_even_without_ui_text(ctx, monkeypatch):
    project = ctx.manager.store.get('projects', 'project')
    agent_review.production.save_plan(project['canvases'][0], plan(False))
    ctx.manager.store.put('projects', 'project', project)
    key = asset(ctx)
    async def complete(http, config, history, tools, instructions, on_text=None):
        assert on_text is not None, 'Actual gateway requires stream:true even for independent JSON reviews'
        return {'text': json.dumps({'verdict': 'pass', 'checks': [
            {'category': category, 'passed': True, 'evidence': 'Visible source evidence'}
            for category in ('identity', 'continuity', 'action', 'camera', 'artifacts')]}), 'usage': {}}
    monkeypatch.setattr(agent_review, 'complete', complete)
    result = await agent_review.review(ctx, {'asset_id': key, 'purpose': 'master', 'entity_id': 'hero'}, None, {}, lambda _: None)
    assert result['verdict'] == 'pass'


async def test_review_provider_failure_stops_expensive_expansion(ctx, monkeypatch):
    async def review(_):
        raise ProviderError('unsupported provider wire contract', status=400, retryable=False)
    ctx.review = review
    with pytest.raises(agent_review.ReviewUnavailable, match='审片服务'):
        await agent_review.invoke(ctx, {'asset_id': 'source', 'purpose': 'master'})


def test_shared_budget_cannot_reset_when_new_project_or_session_starts(ctx):
    store = ctx.manager.store
    store.put('production_budgets', ctx.account, {'authorization_id': 'original-user-budget', 'balance_before': 700, 'limit_cu': 100})
    store.put('accounts', ctx.account, {'usage': {'balance': 650}})
    assert agent_review.production.budget(store, ctx.account)['remaining_cu'] == 50
    store.put('projects', 'another', {'id': 'another'})
    assert agent_review.production.budget(store, ctx.account)['remaining_cu'] == 50
    store.put('accounts', ctx.account, {'usage': {'balance': 604}})
    with pytest.raises(ValueError, match='共享预算'):
        agent_review.production.guard_budget(store, ctx.account)
