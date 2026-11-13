"""Independent regressions for paid-request and destructive-action boundaries."""
import asyncio
import json

import pytest
from aiohttp import web

from backend.agent_provider import ProviderError, _parse_response, complete
from tests.test_agent_provider import INSTRUCTIONS, TOOLS, config, provider, response
from tests.test_agent_service import setup_client, turn, wait_run


async def test_accepted_json_response_disconnect_never_reissues_billable_request():
    requests = []
    async def handle(request):
        requests.append(await request.json())
        if len(requests) > 1:
            return web.json_response(response('Repeated paid request'))
        stream = web.StreamResponse(headers={'Content-Type': 'application/json', 'Content-Length': '10000'})
        await stream.prepare(request)
        await stream.write(b'{"id":"already-accepted","status":"completed",')
        request.transport.close()
        return stream
    async with provider(handle) as (http, base):
        with pytest.raises(ProviderError):
            await complete(http, config(base), [{'role': 'user', 'content': 'Run once'}], TOOLS, INSTRUCTIONS)
    assert len(requests) == 1


@pytest.mark.parametrize('kind', ['function_call', 'message'])
@pytest.mark.parametrize('status', ['incomplete', 'in_progress'])
def test_response_completed_never_executes_explicitly_unfinished_output(kind, status):
    item = ({'type': 'function_call', 'call_id': 'paid', 'name': 'generate_nodes', 'arguments': '{}'}
            if kind == 'function_call' else {'type': 'message', 'role': 'assistant',
                'content': [{'type': 'output_text', 'text': 'Not finished'}]})
    item['status'] = status
    with pytest.raises(ProviderError):
        _parse_response({'id': 'r', 'status': 'completed', 'output': [item]}, 'responses')


def test_completed_output_items_remain_valid_and_preserve_reasoning():
    output = [{'type': 'reasoning', 'id': 'r', 'summary': []},
              {'type': 'function_call', 'status': 'completed', 'call_id': 'safe',
               'name': 'inspect_canvas', 'arguments': json.dumps({})}]
    parsed = _parse_response({'id': 'r', 'status': 'completed', 'output': output}, 'responses')
    assert parsed['tool_calls'] == [{'id': 'safe', 'name': 'inspect_canvas', 'arguments': '{}'}]
    assert parsed['provider_output'] == output


async def test_disconnection_after_request_body_before_headers_never_reissues_paid_request():
    requests = []
    async def handle(request):
        requests.append(await request.json())
        if len(requests) > 1:
            return web.json_response(response('Duplicated accepted request'))
        request.transport.close()
        return web.Response()
    async with provider(handle) as (http, base):
        with pytest.raises(ProviderError):
            await complete(http, config(base), [{'role': 'user', 'content': 'Run once'}], TOOLS, INSTRUCTIONS)
    assert len(requests) == 1


async def test_timeout_after_request_body_never_reissues_paid_request():
    requests = []
    async def handle(request):
        requests.append(await request.json())
        await asyncio.sleep(.5)
        return web.json_response(response())
    async with provider(handle) as (http, base):
        with pytest.raises(ProviderError) as error:
            await complete(http, config(base, timeout_seconds=.25),
                           [{'role': 'user', 'content': 'Run once'}], TOOLS, INSTRUCTIONS)
    assert error.value.retryable is False
    assert len(requests) == 1


async def test_stale_approval_cannot_authorize_a_different_destructive_call(tmp_path, monkeypatch):
    from backend import agent_service
    from backend.agent_tools import ToolApprovalRequired
    effects, model_calls = [], []
    async def responder(*args, **kwargs):
        model_calls.append(True)
        if len(model_calls) == 1:
            return turn(calls=[{'id': key, 'name': 'server', 'arguments': '{"action":"stop"}'}
                               for key in ('stop-first', 'stop-next')])
        return turn('Finished')
    async def dispatch(name, args, ctx):
        if not ctx.approved:
            raise ToolApprovalRequired('server.stop')
        effects.append(ctx.call_id)
        return {'stopped': True}
    app, client, lifecycle, _, session = await setup_client(tmp_path, monkeypatch, responder)
    monkeypatch.setattr(agent_service, 'execute_tool', dispatch)
    try:
        run = (await client.post(f"/api/agent/sessions/{session['id']}/messages", json={
            'message': 'Stop', 'mode': 'auto', 'account': 'colab1', 'request_id': 'approval-audit'})).json()
        await wait_run(client, run['id'], 'awaiting_approval')
        # The approval must name the exact call shown to the user.
        body = {'approved': True, 'call_id': 'stop-first'}
        first = await client.post('/api/agent/runs/' + run['id'] + '/approval', json=body)
        assert first.status_code == 200, first.text
        for _ in range(100):
            state = (await client.get('/api/agent/runs/' + run['id'])).json()
            if (state.get('approval') or {}).get('call_id') == 'stop-next':
                break
            await asyncio.sleep(.01)
        repeated = await client.post('/api/agent/runs/' + run['id'] + '/approval', json=body)
        assert repeated.status_code == 409
        assert effects == ['stop-first']
    finally:
        await client.aclose()
        await lifecycle.__aexit__(None, None, None)
