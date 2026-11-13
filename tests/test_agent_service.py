import asyncio
import json

import pytest
from httpx import ASGITransport, AsyncClient

from backend.app import create_app
from tests.fake_colab import PROFILES, FakeColab


async def setup_client(tmp_path, monkeypatch, responder):
    from backend import agent_service

    monkeypatch.setattr(agent_service, 'complete', responder)
    app = create_app(tmp_path, driver=FakeColab(), account_profiles=PROFILES)
    lifecycle = app.router.lifespan_context(app)
    await lifecycle.__aenter__()
    client = AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver')
    client.headers['x-workbench-token'] = (await client.get('/api/state')).json()['token']
    config = await client.put('/api/agent/settings', json={
        'base_url': 'http://127.0.0.1:9020/v1', 'model': 'test-director', 'protocol': 'responses', 'api_key': 'test-private-key'})
    assert config.status_code == 200, config.text
    project = (await client.post('/api/projects', json={'name': 'Agent tests'})).json()
    session = (await client.post('/api/agent/sessions', json={'project_id': project['id'], 'canvas_id': project['canvases'][0]['id']})).json()
    return app, client, lifecycle, project, session


async def wait_run(client, key, expected='completed'):
    for _ in range(150):
        run = (await client.get('/api/agent/runs/' + key)).json()
        if run['status'] in {'completed', 'failed', 'cancelled', 'awaiting_approval', 'interrupted'}:
            assert run['status'] == expected, run
            return run
        await asyncio.sleep(.01)
    pytest.fail('Agent did not settle')


def turn(text='', calls=None, usage=None):
    return {'text': text, 'tool_calls': calls or [], 'provider_output': None,
            'usage': usage or {'input_tokens': 1200, 'output_tokens': 40, 'cached_tokens': 1024}, 'response_id': 'r1', 'finish_reason': 'stop'}


async def test_agent_tool_roundtrip_is_durable_and_request_retry_does_not_duplicate(tmp_path, monkeypatch):
    received = []
    async def responder(http, config, history, tools, instructions, on_text=None):
        received.append(json.loads(json.dumps(history)))
        if len(received) == 1:
            return turn(calls=[{'id': 'inspect-1', 'name': 'inspect_canvas', 'arguments': '{}'}])
        assert history[-1]['role'] == 'tool'
        assert 'revision' in json.loads(history[-1]['content'])
        return turn('已检查画布，可以继续设计。')
    app, client, lifecycle, project, session = await setup_client(tmp_path, monkeypatch, responder)
    try:
        body = {'message': '只策划一支一分钟电影', 'mode': 'plan', 'account': 'colab1', 'selected_nodes': [], 'request_id': 'request-1'}
        first = await client.post(f"/api/agent/sessions/{session['id']}/messages", json=body)
        run = await wait_run(client, first.json()['id'])
        repeated = await client.post(f"/api/agent/sessions/{session['id']}/messages", json=body)
        assert repeated.json()['id'] == run['id']
        assert len(received) == 2
        calls = app.state.store.all('agent_calls')
        assert len(calls) == 1 and calls[0]['status'] == 'completed'
        assert run['usage']['cached_tokens'] == 2048
        saved = (await client.get('/api/agent/sessions/' + session['id'])).json()
        assert saved['messages'][-1]['content'] == '已检查画布，可以继续设计。'
        assert not app.state.manager.machine('colab1')['session']
    finally:
        await client.aclose()
        await lifecycle.__aexit__(None, None, None)


async def test_agent_settings_keep_key_private_and_preserve_it_when_omitted(tmp_path, monkeypatch):
    async def responder(*a, **kw):
        return turn('ok')
    app, client, lifecycle, project, session = await setup_client(tmp_path, monkeypatch, responder)
    try:
        saved = await client.get('/api/agent/settings')
        assert saved.json()['has_key'] is True
        assert 'test-private-key' not in saved.text
        assert 'test-private-key' not in (await client.get('/api/state')).text
        response = await client.put('/api/agent/settings', json={'base_url': 'https://example.org/v1', 'model': 'new-model', 'protocol': 'chat_completions'})
        assert response.status_code == 200
        assert json.loads((tmp_path / 'agent-private.json').read_text())['api_key'] == 'test-private-key'
        for url in ['file:///etc/passwd', 'https://name:password@example.org/v1', 'https://example.org/v1?api_key=secret']:
            invalid = await client.put('/api/agent/settings', json={'base_url': url, 'model': 'm', 'protocol': 'responses'})
            assert invalid.status_code in {400, 422}
    finally:
        await client.aclose()
        await lifecycle.__aexit__(None, None, None)


async def test_agent_cancel_and_restart_do_not_reissue_tool_side_effects(tmp_path, monkeypatch):
    started = asyncio.Event()
    async def responder(*a, **kw):
        started.set()
        await asyncio.Event().wait()
    app, client, lifecycle, project, session = await setup_client(tmp_path, monkeypatch, responder)
    try:
        run = (await client.post(f"/api/agent/sessions/{session['id']}/messages", json={
            'message': '设计故事', 'mode': 'plan', 'account': 'colab1', 'request_id': 'cancel-1'})).json()
        await asyncio.wait_for(started.wait(), 2)
        await client.post('/api/agent/runs/' + run['id'] + '/cancel', json={})
        await wait_run(client, run['id'], 'cancelled')
        assert not app.state.store.all('agent_calls')
    finally:
        await client.aclose()
        await lifecycle.__aexit__(None, None, None)


async def test_agent_plan_mode_does_not_generate_even_if_provider_calls_tool(tmp_path, monkeypatch):
    count = 0
    async def responder(http, config, history, tools, instructions, on_text=None):
        nonlocal count
        count += 1
        if count == 1:
            return turn(calls=[{'id': 'paid', 'name': 'generate_nodes', 'arguments': json.dumps({'node_ids': ['x'], 'expected_revision': 0, 'regenerate': True})}])
        assert '策划' in history[-1]['content']
        return turn('已保留策划模式。')
    app, client, lifecycle, project, session = await setup_client(tmp_path, monkeypatch, responder)
    try:
        run = (await client.post(f"/api/agent/sessions/{session['id']}/messages", json={
            'message': '只写提示词，不生成', 'mode': 'plan', 'account': 'colab1', 'request_id': 'plan-1'})).json()
        await wait_run(client, run['id'])
        assert app.state.store.all('jobs') == []
        assert not app.state.manager.colab.prompt_posts
    finally:
        await client.aclose()
        await lifecycle.__aexit__(None, None, None)


async def test_model_test_uses_streaming_gateway(tmp_path, monkeypatch):
    async def responder(*args, on_text=None, **kwargs):
        assert on_text is not None, 'gateway only accepts stream:true'
        return turn('连接正常')
    app, client, lifecycle, project, session = await setup_client(tmp_path, monkeypatch, responder)
    try:
        result = await client.post('/api/agent/test', json={})
        assert result.status_code == 200, result.text
    finally:
        await client.aclose()
        await lifecycle.__aexit__(None, None, None)


async def test_new_user_message_cannot_orphan_interrupted_tool_round(tmp_path, monkeypatch):
    async def responder(*args, **kwargs):
        return turn('unused')
    app, client, lifecycle, project, session = await setup_client(tmp_path, monkeypatch, responder)
    try:
        store = app.state.store
        history = [{'role': 'user', 'content': 'Generate'}, {'role': 'assistant', 'content': '', 'tool_calls': [
            {'id': 'pending', 'name': 'inspect_canvas', 'arguments': '{}'}]}]
        store.patch('agent_sessions', session['id'], history=history)
        response = await client.post(f"/api/agent/sessions/{session['id']}/messages", json={
            'message': 'New request', 'mode': 'auto', 'account': 'colab1', 'request_id': 'new-user'})
        assert response.status_code == 409, response.text
        assert store.get('agent_sessions', session['id'])['history'] == history
        assert store.all('agent_runs') == []
    finally:
        await client.aclose()
        await lifecycle.__aexit__(None, None, None)


async def test_session_model_change_requires_new_conversation(tmp_path, monkeypatch):
    async def responder(*args, **kwargs):
        return turn('finished')
    app, client, lifecycle, project, session = await setup_client(tmp_path, monkeypatch, responder)
    try:
        first = (await client.post(f"/api/agent/sessions/{session['id']}/messages", json={
            'message': 'Plan', 'account': 'colab1', 'request_id': 'first'})).json()
        await wait_run(client, first['id'])
        await client.put('/api/agent/settings', json={'base_url': 'http://127.0.0.1:9020/v1',
            'model': 'different-model', 'protocol': 'responses'})
        result = await client.post(f"/api/agent/sessions/{session['id']}/messages", json={
            'message': 'Continue', 'account': 'colab1', 'request_id': 'second'})
        assert result.status_code == 400, result.text
        assert len(app.state.store.all('agent_runs')) == 1
    finally:
        await client.aclose()
        await lifecycle.__aexit__(None, None, None)


def test_pending_calls_scan_all_rounds_instead_of_only_latest_assistant():
    from backend.agent_service import AgentService

    old = {'id': 'unfinished', 'name': 'inspect_canvas', 'arguments': '{}'}
    history = [{'role': 'assistant', 'content': '', 'tool_calls': [old]},
               {'role': 'user', 'content': 'New request from an old buggy client'},
               {'role': 'assistant', 'content': 'New text', 'tool_calls': []}]
    assert AgentService.pending_calls(history) == [old]


async def test_plan_mode_cannot_connect_gpu_even_if_dispatcher_is_replaced(tmp_path, monkeypatch):
    from backend import agent_service

    calls, provider_calls = [], []
    async def responder(http, config, history, *args, **kwargs):
        provider_calls.append(history)
        if len(provider_calls) == 1:
            return turn(calls=[{'id': 'connect', 'name': 'server', 'arguments': '{"action":"connect"}'}])
        assert '策划' in history[-1]['content'] or '规划' in history[-1]['content']
        return turn('没有申请 GPU')
    async def dispatch(name, args, ctx):
        calls.append(name)
        return {'allocated': True}
    app, client, lifecycle, project, session = await setup_client(tmp_path, monkeypatch, responder)
    monkeypatch.setattr(agent_service, 'execute_tool', dispatch)
    try:
        run = (await client.post(f"/api/agent/sessions/{session['id']}/messages", json={
            'message': 'Only plan', 'account': 'colab1', 'request_id': 'plan-connect'})).json()
        await wait_run(client, run['id'])
        assert calls == []
    finally:
        await client.aclose()
        await lifecycle.__aexit__(None, None, None)


@pytest.mark.parametrize('changed', ['name', 'arguments'])
async def test_reused_call_id_rejects_different_ledger_identity(tmp_path, monkeypatch, changed):
    from backend import agent_service

    count, effects = 0, []
    async def responder(http, config, history, *args, **kwargs):
        nonlocal count
        count += 1
        if count == 1:
            return turn(calls=[{'id': 'same-call', 'name': 'inspect_canvas', 'arguments': '{}'}])
        if count == 2:
            call = {'id': 'same-call', 'name': 'inspect_canvas', 'arguments': '{}'}
            call[changed] = 'inspect_assets' if changed == 'name' else '{"include_versions":true}'
            return turn(calls=[call])
        return turn('unexpected third provider call')
    async def dispatch(name, args, ctx):
        effects.append((name, args))
        return {'ok': True}
    app, client, lifecycle, project, session = await setup_client(tmp_path, monkeypatch, responder)
    monkeypatch.setattr(agent_service, 'execute_tool', dispatch)
    try:
        run = (await client.post(f"/api/agent/sessions/{session['id']}/messages", json={
            'message': 'Read', 'account': 'colab1', 'request_id': 'conflict'})).json()
        result = await wait_run(client, run['id'], 'failed')
        assert '一致' in result['message'] or '冲突' in result['message']
        assert len(effects) == 1
    finally:
        await client.aclose()
        await lifecycle.__aexit__(None, None, None)


async def test_job_wait_remains_local_until_completion_and_is_cancellable(tmp_path, monkeypatch):
    from backend import agent_service

    seen, probes = [], []
    finish = asyncio.Event()
    async def responder(http, config, history, *args, **kwargs):
        seen.append(history)
        if len(seen) == 1:
            return turn(calls=[{'id': 'wait-job', 'name': 'get_jobs', 'arguments': '{"job_ids":["job-one"],"wait_seconds":0}'}])
        assert json.loads(history[-1]['content'])['all_terminal']
        return turn('finished')
    async def dispatch(name, args, ctx):
        probes.append(args)
        return {'job_ids': ['job-one'], 'jobs': [{'id': 'job-one', 'status': 'completed' if finish.is_set() else 'running'}],
                'all_terminal': finish.is_set(), 'all_completed': finish.is_set()}
    app, client, lifecycle, project, session = await setup_client(tmp_path, monkeypatch, responder)
    monkeypatch.setattr(agent_service, 'execute_tool', dispatch)
    try:
        run = (await client.post(f"/api/agent/sessions/{session['id']}/messages", json={
            'message': 'Wait for it', 'mode': 'auto', 'account': 'colab1', 'request_id': 'wait-local'})).json()
        for _ in range(40):
            if probes:
                break
            await asyncio.sleep(.01)
        await asyncio.sleep(.08)
        assert len(seen) == 1, 'GPU polling must not consume repeated model calls'
        finish.set()
        await wait_run(client, run['id'])
        assert len(seen) == 2
        assert all(args.get('wait') is False for args in probes)
    finally:
        await client.aclose()
        await lifecycle.__aexit__(None, None, None)


async def test_compaction_keeps_full_tool_rounds_and_accumulates_its_usage(tmp_path, monkeypatch):

    compact_calls = []
    async def responder(http, config, history, tools, instructions, on_text=None):
        assert on_text is not None
        if tools:
            return turn('finished')
        compact_calls.append(history)
        return turn('Complete checkpoint with actual IDs and remaining goals', usage={
            'input_tokens': 900, 'output_tokens': 100, 'cached_tokens': 600})
    app, client, lifecycle, project, session = await setup_client(tmp_path, monkeypatch, responder)
    try:
        agent = app.state.agent
        history = [{'role': 'user', 'content': 'x' * 241000}]
        for index in range(7):
            history.extend([{'role': 'assistant', 'content': '', 'tool_calls': [
                {'id': f'c{index}', 'name': 'inspect_canvas', 'arguments': '{}'}]},
                {'role': 'tool', 'name': 'inspect_canvas', 'tool_call_id': f'c{index}', 'content': '{"revision":1}'}])
        app.state.store.put('agent_runs', 'compact-run', {'id': 'compact-run', 'session_id': session['id'],
            'status': 'running', 'usage': {'input_tokens': 100, 'output_tokens': 20, 'cached_tokens': 80}})
        session = app.state.store.patch('agent_sessions', session['id'], active_run_id='compact-run')
        compacted = await agent.compact(session, history, agent.config())
        assert compact_calls
        checkpoint = app.state.store.get('agent_sessions', session['id'])['checkpoints'][-1]
        assert checkpoint['history'] == history
        assert checkpoint['summary'] in compacted[0]['content']
        ids = {call['id'] for message in compacted for call in message.get('tool_calls', [])}
        assert {message['tool_call_id'] for message in compacted if message['role'] == 'tool'} == ids
        assert compacted[1:] and compacted[1]['role'] == 'assistant'
        assert app.state.store.get('agent_runs', 'compact-run')['usage'] == {
            'input_tokens': 1000, 'output_tokens': 120, 'cached_tokens': 680}
    finally:
        await client.aclose()
        await lifecycle.__aexit__(None, None, None)


@pytest.mark.parametrize('approved', [True, False])
async def test_approval_survives_service_restart_and_never_executes_without_decision(tmp_path, monkeypatch, approved):
    from backend import agent_service

    provider_calls, effects = [], []
    async def responder(http, config, history, *args, **kwargs):
        provider_calls.append(history)
        if len(provider_calls) == 1:
            return turn(calls=[{'id': 'stop-gpu', 'name': 'server', 'arguments': '{"action":"stop"}'}])
        result = json.loads(history[-1]['content'])
        assert ('stopped' in result) is approved
        return turn('已处理确认结果')
    async def dispatch(name, args, ctx):
        if not ctx.approved:
            raise agent_service.ToolApprovalRequired('server.stop')
        effects.append(ctx.call_id)
        return {'stopped': True}
    app, client, lifecycle, project, session = await setup_client(tmp_path, monkeypatch, responder)
    monkeypatch.setattr(agent_service, 'execute_tool', dispatch)
    try:
        run = (await client.post(f"/api/agent/sessions/{session['id']}/messages", json={
            'message': 'Stop GPU', 'mode': 'auto', 'account': 'colab1', 'request_id': 'approve-restart'})).json()
        await wait_run(client, run['id'], 'awaiting_approval')
        assert app.state.store.all('agent_calls')[0]['status'] == 'awaiting_approval'
        await app.state.agent.close()
        app.state.agent = agent_service.AgentService(app.state.manager)
        result = await client.post('/api/agent/runs/' + run['id'] + '/resume', json={})
        assert result.status_code == 202, result.text
        await wait_run(client, run['id'], 'awaiting_approval')
        assert effects == [] and len(provider_calls) == 1
        result = await client.post('/api/agent/runs/' + run['id'] + '/approval', json={'approved': approved, 'call_id': 'stop-gpu'})
        assert result.status_code == 200, result.text
        await wait_run(client, run['id'])
        assert effects == (['stop-gpu'] if approved else [])
        ledger = app.state.store.all('agent_calls')[0]
        assert ledger['status'] == 'completed' and ledger['approval_decision'] is approved
        assert len(provider_calls) == 2
        repeated = await client.post('/api/agent/runs/' + run['id'] + '/approval', json={'approved': approved, 'call_id': 'stop-gpu'})
        assert repeated.status_code == 400
    finally:
        await client.aclose()
        await lifecycle.__aexit__(None, None, None)


@pytest.mark.parametrize('approved', [True, False])
async def test_persisted_approval_decision_resumes_without_second_confirmation(tmp_path, monkeypatch, approved):
    from backend import agent_service

    provider_calls, effects = [], []
    async def responder(http, config, history, *args, **kwargs):
        provider_calls.append(history)
        if len(provider_calls) == 1:
            return turn(calls=[{'id': 'stop-gpu', 'name': 'server', 'arguments': '{"action":"stop"}'}])
        return turn('done')
    async def dispatch(name, args, ctx):
        if not ctx.approved:
            raise agent_service.ToolApprovalRequired('server.stop')
        effects.append(ctx.call_id)
        return {'stopped': True}
    app, client, lifecycle, project, session = await setup_client(tmp_path, monkeypatch, responder)
    monkeypatch.setattr(agent_service, 'execute_tool', dispatch)
    try:
        run = (await client.post(f"/api/agent/sessions/{session['id']}/messages", json={
            'message': 'Stop GPU', 'mode': 'auto', 'account': 'colab1', 'request_id': 'decision-restart'})).json()
        await wait_run(client, run['id'], 'awaiting_approval')
        await app.state.agent.close()
        ledger = app.state.store.all('agent_calls')[0]
        app.state.store.patch('agent_calls', ledger['id'], approval_decision=approved)
        app.state.agent = agent_service.AgentService(app.state.manager)
        await client.post('/api/agent/runs/' + run['id'] + '/resume', json={})
        await wait_run(client, run['id'])
        assert effects == (['stop-gpu'] if approved else [])
        assert len(provider_calls) == 2
    finally:
        await client.aclose()
        await lifecycle.__aexit__(None, None, None)


async def test_started_generation_recovers_submitted_job_after_restart(tmp_path, monkeypatch):
    from backend import agent_service

    submitted = asyncio.Event()
    provider_calls, effects = [], []
    async def responder(http, config, history, *args, **kwargs):
        provider_calls.append(history)
        if len(provider_calls) == 1:
            return turn(calls=[{'id': 'paid-call', 'name': 'generate_nodes', 'arguments': '{"node_ids":["x"],"expected_revision":1}'}])
        result = json.loads(history[-1]['content'])
        assert result['job_ids'] == ['previous-job'] and result['recovered']
        return turn('已恢复原任务')
    async def dispatch(name, args, ctx):
        effects.append(ctx.call_id)
        ctx.manager.store.put('jobs', 'previous-job', {'id': 'previous-job', 'account': 'colab1', 'status': 'completed',
            'creative': {'agent_run_id': ctx.run_id, 'agent_call_id': ctx.call_id}})
        submitted.set()
        await asyncio.Event().wait()
    app, client, lifecycle, project, session = await setup_client(tmp_path, monkeypatch, responder)
    monkeypatch.setattr(agent_service, 'execute_tool', dispatch)
    try:
        run = (await client.post(f"/api/agent/sessions/{session['id']}/messages", json={
            'message': 'Generate', 'mode': 'auto', 'account': 'colab1', 'request_id': 'paid-restart'})).json()
        await asyncio.wait_for(submitted.wait(), 2)
        await app.state.agent.close()
        assert app.state.store.all('agent_calls')[0]['status'] == 'started'
        app.state.agent = agent_service.AgentService(app.state.manager)
        await client.post('/api/agent/runs/' + run['id'] + '/resume', json={})
        await wait_run(client, run['id'])
        assert len(effects) == 1 and len(provider_calls) == 2
        assert len(app.state.store.all('jobs')) == 1
    finally:
        await client.aclose()
        await lifecycle.__aexit__(None, None, None)


async def test_local_job_wait_can_cancel_and_resume_without_model_polling(tmp_path, monkeypatch):
    from backend import agent_service

    waiting, finish = asyncio.Event(), asyncio.Event()
    seen, probes = [], []
    async def responder(http, config, history, *args, **kwargs):
        seen.append(history)
        if len(seen) == 1:
            return turn(calls=[{'id': 'wait', 'name': 'get_jobs', 'arguments': '{"job_ids":["existing-job"]}'}])
        assert json.loads(history[-1]['content'])['all_terminal']
        return turn('finished')
    async def dispatch(name, args, ctx):
        probes.append(args)
        waiting.set()
        return {'all_terminal': finish.is_set(), 'jobs': [{'id': 'existing-job', 'status': 'completed' if finish.is_set() else 'running'}]}
    app, client, lifecycle, project, session = await setup_client(tmp_path, monkeypatch, responder)
    monkeypatch.setattr(agent_service, 'execute_tool', dispatch)
    try:
        run = (await client.post(f"/api/agent/sessions/{session['id']}/messages", json={
            'message': 'Wait', 'mode': 'auto', 'account': 'colab1', 'request_id': 'cancel-wait'})).json()
        await asyncio.wait_for(waiting.wait(), 2)
        await client.post('/api/agent/runs/' + run['id'] + '/cancel', json={})
        await wait_run(client, run['id'], 'cancelled')
        assert len(seen) == 1
        assert app.state.store.all('agent_calls')[0]['status'] == 'started'
        finish.set()
        resumed = await client.post('/api/agent/runs/' + run['id'] + '/resume', json={})
        assert resumed.status_code == 202, resumed.text
        await wait_run(client, run['id'])
        assert len(seen) == 2 and len(probes) == 2
        assert all(probe['wait'] is False for probe in probes)
        assert not app.state.store.all('jobs')
    finally:
        await client.aclose()
        await lifecycle.__aexit__(None, None, None)


async def test_unknown_mutation_after_crash_stops_with_visible_closed_tool_result(tmp_path, monkeypatch):
    from backend import agent_service

    started = asyncio.Event()
    effects, provider_calls = [], []
    async def responder(http, config, history, *args, **kwargs):
        provider_calls.append(history)
        return turn(calls=[{'id': 'edit', 'name': 'edit_canvas', 'arguments': '{"expected_revision":1,"operations":[]}'}])
    async def dispatch(name, args, ctx):
        effects.append(name)
        started.set()
        await asyncio.Event().wait()
    app, client, lifecycle, project, session = await setup_client(tmp_path, monkeypatch, responder)
    monkeypatch.setattr(agent_service, 'execute_tool', dispatch)
    try:
        run = (await client.post(f"/api/agent/sessions/{session['id']}/messages", json={
            'message': 'Edit', 'mode': 'auto', 'account': 'colab1', 'request_id': 'uncertain'})).json()
        await asyncio.wait_for(started.wait(), 2)
        await app.state.agent.close()
        app.state.agent = agent_service.AgentService(app.state.manager)
        await client.post('/api/agent/runs/' + run['id'] + '/resume', json={})
        result = await wait_run(client, run['id'], 'failed')
        assert '不确定' in result['message']
        assert len(effects) == 1 and len(provider_calls) == 1
        history = app.state.store.get('agent_sessions', session['id'])['history']
        assert history[-1]['role'] == 'tool' and json.loads(history[-1]['content'])['uncertain']
        assert agent_service.AgentService.pending_calls(history) == []
    finally:
        await client.aclose()
        await lifecycle.__aexit__(None, None, None)


async def test_saved_final_answer_after_crash_completes_without_calling_provider(tmp_path, monkeypatch):
    async def responder(*args, **kwargs):
        return turn('saved final')
    app, client, lifecycle, project, session = await setup_client(tmp_path, monkeypatch, responder)
    try:
        run = (await client.post(f"/api/agent/sessions/{session['id']}/messages", json={
            'message': 'Plan', 'account': 'colab1', 'request_id': 'finished'})).json()
        await wait_run(client, run['id'])
        app.state.store.patch('agent_runs', run['id'], status='interrupted')
        async def must_not_call(*args, **kwargs):
            raise AssertionError('Saved final answer must not be purchased twice')
        monkeypatch.setattr('backend.agent_service.complete', must_not_call)
        resumed = await client.post('/api/agent/runs/' + run['id'] + '/resume', json={})
        assert resumed.status_code == 202
        await wait_run(client, run['id'])
    finally:
        await client.aclose()
        await lifecycle.__aexit__(None, None, None)


async def test_old_run_cannot_resume_over_newer_conversation(tmp_path, monkeypatch):
    started = asyncio.Event()
    async def responder(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()
    app, client, lifecycle, project, session = await setup_client(tmp_path, monkeypatch, responder)
    try:
        old = (await client.post(f"/api/agent/sessions/{session['id']}/messages", json={
            'message': 'Old', 'account': 'colab1', 'request_id': 'old'})).json()
        await asyncio.wait_for(started.wait(), 2)
        await client.post('/api/agent/runs/' + old['id'] + '/cancel', json={})
        async def final(*args, **kwargs):
            return turn('new finished')
        monkeypatch.setattr('backend.agent_service.complete', final)
        new = (await client.post(f"/api/agent/sessions/{session['id']}/messages", json={
            'message': 'New', 'account': 'colab1', 'request_id': 'new'})).json()
        await wait_run(client, new['id'])
        resumed = await client.post('/api/agent/runs/' + old['id'] + '/resume', json={})
        assert resumed.status_code == 409, resumed.text
        assert app.state.store.get('agent_sessions', session['id'])['last_run_id'] == new['id']
    finally:
        await client.aclose()
        await lifecycle.__aexit__(None, None, None)


@pytest.mark.parametrize('changes', [{'mode': 'auto'}, {'account': 'colab2'}])
async def test_request_retry_cannot_change_execution_context(tmp_path, monkeypatch, changes):
    async def responder(*args, **kwargs):
        return turn('done')
    app, client, lifecycle, project, session = await setup_client(tmp_path, monkeypatch, responder)
    try:
        body = {'message': 'Plan', 'mode': 'plan', 'account': 'colab1', 'request_id': 'context-id'}
        run = (await client.post(f"/api/agent/sessions/{session['id']}/messages", json=body)).json()
        await wait_run(client, run['id'])
        result = await client.post(f"/api/agent/sessions/{session['id']}/messages", json={**body, **changes})
        assert result.status_code == 409
        assert len(app.state.store.all('agent_runs')) == 1
    finally:
        await client.aclose()
        await lifecycle.__aexit__(None, None, None)


async def test_blank_message_never_starts_a_paid_call(tmp_path, monkeypatch):
    async def responder(*args, **kwargs):
        pytest.fail('Whitespace must not call the provider')
    app, client, lifecycle, project, session = await setup_client(tmp_path, monkeypatch, responder)
    try:
        result = await client.post(f"/api/agent/sessions/{session['id']}/messages", json={
            'message': '  \n ', 'account': 'colab1', 'request_id': 'blank'})
        assert result.status_code == 400
        assert app.state.store.all('agent_runs') == []
    finally:
        await client.aclose()
        await lifecycle.__aexit__(None, None, None)


async def test_canvas_settings_reject_strict_schema_that_the_tools_cannot_honor(tmp_path, monkeypatch):
    async def responder(*a, **kw):
        return turn('ok')
    app, client, lifecycle, project, session = await setup_client(tmp_path, monkeypatch, responder)
    try:
        result = await client.put('/api/agent/settings', json={
            'base_url': 'https://example.org/v1', 'model': 'test', 'strict_tools': True})
        assert result.status_code == 400 and '本地完整校验' in result.text
        assert app.state.agent.config()['strict_tools'] is False
    finally:
        await client.aclose()
        await lifecycle.__aexit__(None, None, None)


@pytest.mark.parametrize('mode,finish', [('auto', True), ('auto', False), ('plan', False)])
async def test_wait_recovers_original_worker_or_reports_blocked_recovery(tmp_path, monkeypatch, mode, finish):
    turns, resumed = [], []
    async def responder(http, config, history, *args, **kwargs):
        turns.append(history)
        if len(turns) == 1:
            return turn(calls=[{'id': 'original-wait', 'name': 'get_jobs', 'arguments': '{"job_ids":["original-job"]}'}])
        result = json.loads(history[-1]['content'])
        if finish:
            assert result['all_completed'] is True
        else:
            assert result['error'] and result['retryable'] is True
        return turn('Original job checked')
    app, client, lifecycle, project, session = await setup_client(tmp_path, monkeypatch, responder)
    manager = app.state.manager
    manager.store.put('jobs', 'original-job', {'id': 'original-job', 'status': 'recovering', 'account': 'colab1',
        'prompt_id': 'original-cloud-prompt', 'created_at': 1, 'outputs': [],
        'creative': {'project_id': project['id'], 'canvas_id': session['canvas_id']}})
    def resume_original(key):
        resumed.append(key)
        async def worker():
            await asyncio.sleep(.05)
            if finish:
                manager.job(key, status='completed')
            else:
                manager.machine('colab1', phase='error', message='Original GPU cannot be reached')
        manager.workers['colab1'] = asyncio.create_task(worker())
        return manager.job(key)
    monkeypatch.setattr(manager, 'resume', resume_original)
    try:
        run = (await client.post(f"/api/agent/sessions/{session['id']}/messages", json={
            'message': 'Continue original wait', 'mode': mode, 'account': 'colab1', 'request_id': 'recover-wait'})).json()
        await wait_run(client, run['id'])
        assert resumed == (['original-job'] if mode == 'auto' else [])
        assert len(manager.store.all('jobs')) == 1
        assert manager.job('original-job')['prompt_id'] == 'original-cloud-prompt'
        assert len(turns) == 2
        assert not any(call[1] == 'create' for call in manager.colab.calls)
    finally:
        await client.aclose()
        await lifecycle.__aexit__(None, None, None)
