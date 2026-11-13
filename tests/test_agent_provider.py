import asyncio
import json
from contextlib import asynccontextmanager

import aiohttp
import pytest
from aiohttp import web

from backend.agent_provider import ProviderError, complete

INSTRUCTIONS = 'Stable director instructions. Inspect the canvas before editing.'
TOOLS = [{'name': 'read_canvas', 'description': 'Read the current canvas.', 'parameters': {
    'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': False}}]


@asynccontextmanager
async def provider(handler):
    app = web.Application()
    app.router.add_post('/{path:.*}', handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        async with aiohttp.ClientSession() as http:
            yield http, f'http://127.0.0.1:{port}/v1'
    finally:
        await runner.cleanup()


def config(base_url, protocol='responses', **extra):
    return {'base_url': base_url, 'api_key': 'test-provider-secret', 'model': 'test-model',
            'protocol': protocol, **extra}


def response(text='Ready', **extra):
    return {'id': 'resp_one', 'status': 'completed', 'output': [
        {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': text}]}], **extra}


@pytest.mark.parametrize('protocol,path', [('responses', '/responses'), ('chat_completions', '/chat/completions')])
@pytest.mark.parametrize('full_path', [False, True])
async def test_precise_endpoint_payload_and_nullable_usage(protocol, path, full_path):
    observed = []
    async def handle(request):
        observed.append((request.path, await request.json(), request.headers['Authorization']))
        return web.json_response(response() if protocol == 'responses' else {
            'id': 'chat_one', 'choices': [{'message': {'role': 'assistant', 'content': 'Ready'}, 'finish_reason': 'stop'}]})
    async with provider(handle) as (http, base):
        result = await complete(http, config(base + path if full_path else base, protocol),
                                [{'role': 'user', 'content': 'Make a story'}], TOOLS, INSTRUCTIONS)
    target, payload, authorization = observed[0]
    assert target == '/v1' + path
    assert authorization == 'Bearer test-provider-secret'
    assert result['text'] == 'Ready'
    assert result['usage'] == {'input_tokens': None, 'output_tokens': None, 'cached_tokens': None}
    assert 'prompt_cache_options' not in payload and 'prompt_cache_key' not in payload
    assert payload['model'] == 'test-model'
    if protocol == 'responses':
        assert payload['instructions'] == INSTRUCTIONS
        assert payload['input'] == [{'role': 'user', 'content': 'Make a story'}]
        assert payload['tools'][0]['name'] == 'read_canvas'
        assert payload['tools'][0]['strict'] is False
        assert payload['max_output_tokens'] == 8192
    else:
        assert payload['messages'] == [{'role': 'system', 'content': INSTRUCTIONS},
                                       {'role': 'user', 'content': 'Make a story'}]
        assert payload['tools'][0]['function']['name'] == 'read_canvas'
        assert payload['tools'][0]['function']['strict'] is False
        assert 'instructions' not in payload


async def test_responses_preserves_reasoning_history_multiple_calls_and_actual_usage():
    native = [{'type': 'reasoning', 'id': 'rs_old', 'encrypted_content': 'opaque', 'summary': []},
              {'type': 'function_call', 'id': 'fc_old', 'call_id': 'call_old', 'name': 'read_canvas', 'arguments': '{}'}]
    output = [{'type': 'reasoning', 'id': 'rs_new', 'encrypted_content': 'opaque2', 'summary': []},
              {'type': 'function_call', 'id': 'fc_a', 'call_id': 'call_a', 'name': 'read_canvas', 'arguments': '{}'},
              {'type': 'function_call', 'id': 'fc_b', 'call_id': 'call_b', 'name': 'create_nodes', 'arguments': '{"title":"夜"}'}]
    history = [{'role': 'user', 'content': 'Plan'}, {'role': 'assistant', 'content': '', 'tool_calls': [
        {'id': 'call_old', 'name': 'read_canvas', 'arguments': '{}'}], 'provider_output': native},
        {'role': 'tool', 'tool_call_id': 'call_old', 'name': 'read_canvas', 'content': '{"revision":1}'}]
    async def handle(request):
        payload = await request.json()
        assert payload['input'][1:3] == native
        assert payload['input'][-1] == {'type': 'function_call_output', 'call_id': 'call_old', 'output': '{"revision":1}'}
        assert payload['prompt_cache_key'] == 'director-v1'
        assert payload['prompt_cache_options'] == {'ttl': '30m'}
        assert payload['tools'][0]['strict'] is True
        return web.json_response(response(output=output, usage={
            'input_tokens': 2000, 'output_tokens': 80, 'input_tokens_details': {'cached_tokens': 1600}}))
    async with provider(handle) as (http, base):
        result = await complete(http, config(base, strict_tools=True, cache_key='director-v1',
                                            cache_options={'ttl': '30m'}), history, TOOLS, INSTRUCTIONS)
    assert result['provider_output'] == output
    assert result['tool_calls'] == [{'id': 'call_a', 'name': 'read_canvas', 'arguments': '{}'},
                                     {'id': 'call_b', 'name': 'create_nodes', 'arguments': '{"title":"夜"}'}]
    assert result['usage'] == {'input_tokens': 2000, 'output_tokens': 80, 'cached_tokens': 1600}
    assert history[1]['provider_output'] == native


async def test_chat_ignores_native_provider_output_and_pairs_tool_results():
    async def handle(request):
        payload = await request.json()
        assistant = payload['messages'][2]
        assert assistant == {'role': 'assistant', 'content': None, 'tool_calls': [
            {'id': 'call_old', 'type': 'function', 'function': {'name': 'read_canvas', 'arguments': '{}'}}]}
        assert payload['messages'][3] == {'role': 'tool', 'tool_call_id': 'call_old', 'content': '{"ok":true}'}
        assert all('provider_output' not in item for item in payload['messages'])
        return web.json_response({'id': 'chat_new', 'choices': [{'message': {'content': None, 'tool_calls': [
            {'id': 'call_a', 'type': 'function', 'function': {'name': 'read_canvas', 'arguments': '{}'}}]},
            'finish_reason': 'tool_calls'}], 'usage': {'prompt_tokens': 1800, 'completion_tokens': 100,
            'prompt_tokens_details': {'cached_tokens': 1200}}})
    history = [{'role': 'user', 'content': 'Plan'}, {'role': 'assistant', 'content': '', 'tool_calls': [
        {'id': 'call_old', 'name': 'read_canvas', 'arguments': '{}'}], 'provider_output': [{'type': 'reasoning'}]},
        {'role': 'tool', 'tool_call_id': 'call_old', 'name': 'read_canvas', 'content': '{"ok":true}'}]
    async with provider(handle) as (http, base):
        result = await complete(http, config(base, 'chat_completions'), history, TOOLS, INSTRUCTIONS)
    assert result['provider_output'] is None
    assert result['tool_calls'] == [{'id': 'call_a', 'name': 'read_canvas', 'arguments': '{}'}]
    assert result['usage'] == {'input_tokens': 1800, 'output_tokens': 100, 'cached_tokens': 1200}


async def sse(request, events):
    stream = web.StreamResponse(headers={'Content-Type': 'text/event-stream'})
    await stream.prepare(request)
    encoded = b': keepalive\r\n\r\n' + b''.join(
        (('data: ' + (item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)) + '\r\n\r\n').encode())
        for item in events)
    for start in range(0, len(encoded), 7):
        await stream.write(encoded[start:start + 7])
    await stream.write_eof()
    return stream


async def test_responses_sse_fragmented_utf8_and_tool_arguments_return_only_on_completed():
    output = [{'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': '完成'}]},
              {'type': 'function_call', 'id': 'fc_one', 'call_id': 'call_one', 'name': 'create_nodes', 'arguments': '{"title":"夜"}'}]
    events = [
        {'type': 'response.created', 'response': {'id': 'resp_one', 'status': 'in_progress'}},
        {'type': 'response.output_text.delta', 'delta': '完'},
        {'type': 'response.output_item.added', 'output_index': 1, 'item': {**output[1], 'arguments': ''}},
        {'type': 'response.function_call_arguments.delta', 'output_index': 1, 'delta': '{"title":'},
        {'type': 'response.output_text.delta', 'delta': '成'},
        {'type': 'response.function_call_arguments.delta', 'output_index': 1, 'delta': '"夜"}'},
        {'type': 'response.output_item.done', 'output_index': 1, 'item': output[1]},
        {'type': 'response.completed', 'response': response('完成', output=output,
            usage={'input_tokens': 100, 'output_tokens': 30, 'input_tokens_details': {'cached_tokens': 0}})}]
    async def handle(request):
        assert (await request.json())['stream'] is True
        return await sse(request, events)
    pieces = []
    async def callback(text):
        await asyncio.sleep(0)
        pieces.append(text)
    async with provider(handle) as (http, base):
        result = await complete(http, config(base), [{'role': 'user', 'content': 'Create'}], TOOLS, INSTRUCTIONS, callback)
    assert pieces == ['完', '成']
    assert result['text'] == '完成'
    assert result['tool_calls'][0]['arguments'] == '{"title":"夜"}'
    assert result['provider_output'] == output


async def test_chat_sse_interleaved_calls_usage_only_chunk_and_done():
    events = [
        {'id': 'chat_stream', 'choices': [{'index': 0, 'delta': {'content': '安排', 'tool_calls': [
            {'index': 0, 'id': 'call_a', 'type': 'function', 'function': {'name': 'read_canvas', 'arguments': '{'}},
            {'index': 1, 'id': 'call_b', 'type': 'function', 'function': {'name': 'create_nodes', 'arguments': '{"title":'}}]}}]},
        {'id': 'chat_stream', 'choices': [{'index': 0, 'delta': {'tool_calls': [
            {'index': 1, 'function': {'arguments': '"夜"}'}}, {'index': 0, 'function': {'arguments': '}'}}]}}]},
        {'id': 'chat_stream', 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'tool_calls'}]},
        {'id': 'chat_stream', 'choices': [], 'usage': {'prompt_tokens': 1200, 'completion_tokens': 30,
            'prompt_tokens_details': {'cached_tokens': 1024}}}, '[DONE]']
    async def handle(request):
        assert (await request.json())['stream_options'] == {'include_usage': True}
        return await sse(request, events)
    pieces = []
    async with provider(handle) as (http, base):
        result = await complete(http, config(base, 'chat_completions'), [], TOOLS, INSTRUCTIONS, pieces.append)
    assert pieces == ['安排']
    assert result['tool_calls'] == [{'id': 'call_a', 'name': 'read_canvas', 'arguments': '{}'},
                                     {'id': 'call_b', 'name': 'create_nodes', 'arguments': '{"title":"夜"}'}]
    assert result['usage']['cached_tokens'] == 1024


@pytest.mark.parametrize('protocol', ['responses', 'chat_completions'])
async def test_truncated_stream_rejects_partial_tool_without_retry(protocol):
    requests = []
    async def handle(request):
        requests.append(await request.json())
        events = [{'type': 'response.output_item.added', 'output_index': 0, 'item': {
            'type': 'function_call', 'call_id': 'call_one', 'id': 'fc_one', 'name': 'read_canvas', 'arguments': '{}'}}]
        if protocol == 'chat_completions':
            events = [{'choices': [{'index': 0, 'delta': {'tool_calls': [{'index': 0, 'id': 'call_one',
                'type': 'function', 'function': {'name': 'read_canvas', 'arguments': '{}'}}]}}]}]
        return await sse(request, events)
    async with provider(handle) as (http, base):
        with pytest.raises(ProviderError, match='中断|完整|结束|终态'):
            await complete(http, config(base, protocol), [], TOOLS, INSTRUCTIONS, lambda _: None)
    assert len(requests) == 1


@pytest.mark.parametrize('protocol', ['responses', 'chat_completions'])
async def test_incomplete_response_rejects_calls_even_if_arguments_parse(protocol):
    async def handle(request):
        if protocol == 'responses':
            return web.json_response(response(status='incomplete', incomplete_details={'reason': 'max_output_tokens'}, output=[
                {'type': 'function_call', 'call_id': 'call_one', 'name': 'read_canvas', 'arguments': '{}'}]))
        return web.json_response({'choices': [{'message': {'tool_calls': [
            {'id': 'call_one', 'type': 'function', 'function': {'name': 'read_canvas', 'arguments': '{}'}}]},
            'finish_reason': 'length'}]})
    async with provider(handle) as (http, base):
        with pytest.raises(ProviderError, match='截断|未完成|不完整'):
            await complete(http, config(base, protocol), [], TOOLS, INSTRUCTIONS)


@pytest.mark.parametrize('status', [400, 401, 403])
async def test_permanent_http_error_is_clear_redacted_and_never_retried(status):
    count = 0
    async def handle(request):
        nonlocal count
        count += 1
        return web.json_response({'error': {'message': 'Invalid key test-provider-secret Authorization: Bearer test-provider-secret'}}, status=status)
    async with provider(handle) as (http, base):
        with pytest.raises(ProviderError) as captured:
            await complete(http, config(base), [], TOOLS, INSTRUCTIONS)
    assert str(status) in str(captured.value)
    assert 'test-provider-secret' not in str(captured.value)
    assert count == 1


async def test_transient_http_retries_same_payload_and_preserves_instructions(monkeypatch):
    payloads = []
    async def handle(request):
        payloads.append(await request.json())
        if len(payloads) < 3:
            return web.json_response({'error': {'message': 'overloaded'}}, status=503, headers={'Retry-After': '0'})
        return web.json_response(response())
    async with provider(handle) as (http, base):
        result = await complete(http, config(base), [], TOOLS, INSTRUCTIONS)
    assert result['text'] == 'Ready'
    assert len(payloads) == 3 and payloads[0] == payloads[1] == payloads[2]


@pytest.mark.parametrize('arguments', ['{', '[1]', 'null'])
async def test_invalid_arguments_never_become_executable_calls(arguments):
    async def handle(request):
        return web.json_response(response(output=[{'type': 'function_call', 'call_id': 'one',
            'name': 'read_canvas', 'arguments': arguments}]))
    async with provider(handle) as (http, base):
        with pytest.raises(ProviderError, match='参数|JSON'):
            await complete(http, config(base), [], TOOLS, INSTRUCTIONS)


async def test_nonfinite_json_tool_arguments_are_rejected():
    async def handle(request):
        return web.json_response(response(output=[{'type': 'function_call', 'call_id': 'one',
            'name': 'read_canvas', 'arguments': '{"value":NaN}'}]))
    async with provider(handle) as (http, base):
        with pytest.raises(ProviderError, match='参数|JSON'):
            await complete(http, config(base), [], TOOLS, INSTRUCTIONS)


@pytest.mark.parametrize('body', [
    {'choices': [{'message': [], 'finish_reason': 'stop'}]},
    {'choices': [{'message': {'tool_calls': ['bad']}, 'finish_reason': 'tool_calls'}]},
    {'choices': [{'message': {'tool_calls': [{'type': 'function', 'function': []}]}, 'finish_reason': 'tool_calls'}]},
    {'choices': [{'message': {'content': [{'type': 'text', 'text': 42}]}, 'finish_reason': 'stop'}]},
])
async def test_malformed_chat_envelopes_raise_controlled_errors(body):
    async def handle(request):
        return web.json_response(body)
    async with provider(handle) as (http, base):
        with pytest.raises(ProviderError):
            await complete(http, config(base, 'chat_completions'), [], TOOLS, INSTRUCTIONS)


async def test_chat_stream_without_usage_keeps_counters_unknown():
    async def handle(request):
        return await sse(request, [
            {'choices': [{'index': 0, 'delta': {'content': 'Ready'}, 'finish_reason': 'stop'}]}, '[DONE]'])
    async with provider(handle) as (http, base):
        result = await complete(http, config(base, 'chat_completions'), [], TOOLS, INSTRUCTIONS, lambda _: None)
    assert result['usage'] == {'input_tokens': None, 'output_tokens': None, 'cached_tokens': None}


async def test_transient_failure_stops_after_two_retries():
    calls = []
    async def handle(request):
        calls.append(request.path)
        return web.json_response({'error': {'message': 'still overloaded'}}, status=503, headers={'Retry-After': '0'})
    async with provider(handle) as (http, base):
        with pytest.raises(ProviderError) as captured:
            await complete(http, config(base), [], TOOLS, INSTRUCTIONS)
    assert captured.value.retryable is True
    assert len(calls) == 3


async def test_redirect_never_forwards_authorization_to_another_path():
    calls = []
    async def handle(request):
        calls.append(request.path)
        return web.Response(status=307, headers={'Location': '/different-host-placeholder'})
    async with provider(handle) as (http, base):
        with pytest.raises(ProviderError) as captured:
            await complete(http, config(base), [], TOOLS, INSTRUCTIONS)
    assert captured.value.status == 307
    assert len(calls) == 1


@pytest.mark.parametrize('url,protocol', [('https://example.com/v1/chat/completions', 'responses'),
    ('https://example.com/v1/responses', 'chat_completions'), ('file:///v1', 'responses'),
    ('https://secret@example.com/v1', 'responses'), ('https://example.com/v1?api_key=bad', 'responses')])
async def test_bad_or_conflicting_endpoints_fail_before_network(url, protocol):
    async with aiohttp.ClientSession() as http:
        with pytest.raises(ProviderError, match='地址|协议|URL|端点'):
            await complete(http, config(url, protocol), [], TOOLS, INSTRUCTIONS)


async def test_static_prefix_and_tool_schema_identical_when_dynamic_history_changes():
    payloads = []
    async def handle(request):
        payloads.append(await request.json())
        return web.json_response(response())
    async with provider(handle) as (http, base):
        await complete(http, config(base), [{'role': 'user', 'content': 'Scene one'}], TOOLS, INSTRUCTIONS)
        await complete(http, config(base), [{'role': 'user', 'content': 'Scene one'},
            {'role': 'assistant', 'content': 'Ready'}, {'role': 'user', 'content': 'Scene two'}], TOOLS, INSTRUCTIONS)
    assert payloads[0]['tools'] == payloads[1]['tools']
    assert payloads[0]['instructions'] == payloads[1]['instructions'] == INSTRUCTIONS
    assert payloads[1]['input'][:1] == payloads[0]['input']


@pytest.mark.parametrize('protocol', ['responses', 'chat_completions'])
async def test_visual_tool_results_follow_all_parallel_tool_outputs(protocol):
    image = {'asset_id': 'abc', 'data_url': 'data:image/jpeg;base64,YWJj'}
    calls = [{'id': 'c1', 'name': 'read_canvas', 'arguments': '{}'},
             {'id': 'c2', 'name': 'read_canvas', 'arguments': '{}'}]
    history = [{'role': 'user', 'content': 'Inspect'}, {'role': 'assistant', 'content': '', 'tool_calls': calls},
               {'role': 'tool', 'tool_call_id': 'c1', 'content': '{}', 'images': [image]},
               {'role': 'tool', 'tool_call_id': 'c2', 'content': '{}'}]
    async def handle(request):
        payload = await request.json()
        assert 'prompt_cache_options' not in payload
        messages = payload['input' if protocol == 'responses' else 'messages']
        assert messages[-2].get('call_id', messages[-2].get('tool_call_id')) == 'c2'
        visual = messages[-1]
        assert visual['role'] == 'user'
        item = visual['content'][-1]
        assert item['type'] == ('input_image' if protocol == 'responses' else 'image_url')
        assert image['data_url'] in json.dumps(item)
        return web.json_response(response('Checked') if protocol == 'responses' else {
            'choices': [{'message': {'content': 'Checked'}, 'finish_reason': 'stop'}]})
    async with provider(handle) as (http, base):
        result = await complete(http, config(base, protocol, cache_options={}), history, TOOLS, INSTRUCTIONS)
    assert result['text'] == 'Checked'
    assert history[-2]['images'] == [image]
