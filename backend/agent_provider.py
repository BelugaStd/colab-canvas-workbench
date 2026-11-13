"""OpenAI-compatible tool calling over Responses and Chat Completions.

Wire contracts: https://developers.openai.com/api/docs/guides/function-calling
Caching: https://developers.openai.com/api/docs/guides/prompt-caching
"""
import asyncio
import codecs
import copy
import inspect
import json
import logging
import re
from urllib.parse import urlsplit, urlunsplit

import aiohttp

MAX_BODY_BYTES = 8 * 1024 * 1024
TRANSIENT_STATUS = {408, 429, 500, 502, 503, 504}


class ProviderError(RuntimeError):
    def __init__(self, message, *, status=None, code='provider_error', retryable=False):
        super().__init__(message)
        self.status, self.code, self.retryable = status, code, retryable


def _endpoint(config):
    protocol = config.get('protocol', 'responses')
    if protocol not in {'responses', 'chat_completions'}:
        raise ProviderError('Agent 协议仅支持 Responses 或 Chat Completions', code='invalid_config')
    raw = str(config.get('base_url', '')).strip()
    try:
        parts = urlsplit(raw)
        if (parts.scheme not in {'http', 'https'} or not parts.hostname or parts.username or parts.password
                or parts.query or parts.fragment):
            raise ValueError()
        parts.port
    except ValueError:
        raise ProviderError('Agent API 地址无效，请填写不含凭据或查询参数的 HTTP(S) URL', code='invalid_config') from None
    target = '/responses' if protocol == 'responses' else '/chat/completions'
    other = '/chat/completions' if protocol == 'responses' else '/responses'
    path = parts.path.rstrip('/')
    if path.endswith(other):
        raise ProviderError('API 端点与所选协议不一致，请检查地址和协议', code='invalid_config')
    if not path.endswith(target):
        path = (path or '/v1') + target
    return urlunsplit((parts.scheme, parts.netloc, path, '', ''))


def _safe_error(value, secret):
    text = str(value)
    if secret:
        text = text.replace(secret, '[redacted]')
    text = re.sub(r'(?i)bearer\s+[^\s,;\"\'}]+', 'Bearer [redacted]', text)
    text = re.sub(r'(?i)(api[_-]?key|token|authorization)([\"\s:=]+)[^\s,;\"\'}]+', r'\1\2[redacted]', text)
    text = re.sub(r'\bsk-[\w-]+', '[redacted]', text)
    return text[:500]


def _messages(history, protocol):
    result = []
    visual = []
    def flush_visual():
        if visual:
            result.append({'role': 'user', 'content': copy.deepcopy(visual)})
            visual.clear()
    def add_visual(message):
        for item in message.get('images', []):
            url = item['data_url']
            if not url.startswith(('data:image/jpeg;base64,', 'data:image/png;base64,', 'data:image/webp;base64,')):
                raise ProviderError('视觉上下文只接受经过校验的本地图片', code='invalid_history')
            label = '工具返回的实际图片，素材 ID：' + item['asset_id']
            if protocol == 'responses':
                visual.extend([{'type': 'input_text', 'text': label}, {'type': 'input_image', 'image_url': url, 'detail': 'auto'}])
            else:
                visual.extend([{'type': 'text', 'text': label}, {'type': 'image_url', 'image_url': {'url': url, 'detail': 'auto'}}])
    for message in history:
        role = message.get('role')
        if role != 'tool':
            flush_visual()
        if role == 'user':
            result.append({'role': 'user', 'content': message.get('content', '')})
        elif role == 'assistant':
            if protocol == 'responses' and isinstance(message.get('provider_output'), list):
                # WARN: Reasoning/opaque items must accompany their original function calls on replay.
                result.extend(copy.deepcopy(message['provider_output']))
                continue
            calls = message.get('tool_calls') or []
            content = message.get('content') or ''
            if protocol == 'responses':
                if content:
                    result.append({'role': 'assistant', 'content': content})
                result.extend({'type': 'function_call', 'call_id': call['id'], 'name': call['name'],
                               'arguments': call['arguments']} for call in calls)
            else:
                item = {'role': 'assistant', 'content': content or (None if calls else '')}
                if calls:
                    item['tool_calls'] = [{'id': call['id'], 'type': 'function', 'function': {
                        'name': call['name'], 'arguments': call['arguments']}} for call in calls]
                result.append(item)
        elif role == 'tool':
            if protocol == 'responses':
                result.append({'type': 'function_call_output', 'call_id': message['tool_call_id'],
                               'output': message.get('content', '')})
            else:
                result.append({'role': 'tool', 'tool_call_id': message['tool_call_id'],
                               'content': message.get('content', '')})
            add_visual(message)
        else:
            raise ProviderError('Agent 历史消息角色无效', code='invalid_history')
    flush_visual()
    return result


def _payload(config, history, tools, instructions, streaming):
    protocol = config.get('protocol', 'responses')
    definitions = [{**copy.deepcopy(tool), 'strict': bool(config.get('strict_tools', False))} for tool in tools]
    payload = {'model': config['model'], 'stream': streaming}
    messages = _messages(history, protocol)
    if protocol == 'responses':
        payload.update(instructions=instructions, input=messages,
                       tools=[{'type': 'function', **tool} for tool in definitions],
                       max_output_tokens=int(config.get('max_output_tokens', 8192)))
    else:
        payload.update(messages=[{'role': 'system', 'content': instructions}, *messages],
                       tools=[{'type': 'function', 'function': tool} for tool in definitions],
                       max_completion_tokens=int(config.get('max_output_tokens', 8192)))
        if streaming:
            payload['stream_options'] = {'include_usage': True}
    if config.get('cache_key'):
        payload['prompt_cache_key'] = config['cache_key']
    if config.get('cache_options'):
        payload['prompt_cache_options'] = copy.deepcopy(config['cache_options'])
    return payload


def _usage(raw, protocol):
    raw = raw if isinstance(raw, dict) else {}
    details = raw.get('input_tokens_details' if protocol == 'responses' else 'prompt_tokens_details')
    details = details if isinstance(details, dict) else {}
    def count(value):
        return value if type(value) is int and value >= 0 else None
    return {'input_tokens': count(raw.get('input_tokens' if protocol == 'responses' else 'prompt_tokens')),
            'output_tokens': count(raw.get('output_tokens' if protocol == 'responses' else 'completion_tokens')),
            'cached_tokens': count(details.get('cached_tokens'))}


def _validate_calls(calls):
    seen = set()
    for call in calls:
        if not isinstance(call.get('id'), str) or not call['id'] or not isinstance(call.get('name'), str) or not call['name']:
            raise ProviderError('模型返回的工具调用缺少标识或名称', code='invalid_tool_call')
        if call['id'] in seen:
            raise ProviderError('模型返回重复的工具调用标识', code='invalid_tool_call')
        seen.add(call['id'])
        try:
            arguments = json.loads(call['arguments'], parse_constant=_reject_constant)
        except (TypeError, ValueError, RecursionError):
            raise ProviderError('模型返回的工具参数不是完整 JSON，请重新请求', code='invalid_tool_call') from None
        if not isinstance(arguments, dict):
            raise ProviderError('模型工具参数必须为 JSON 对象', code='invalid_tool_call')


def _reject_constant(value):
    raise ValueError('JSON contains a nonfinite constant')


def _text_content(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        if any(part.get('type') == 'refusal' for part in content if isinstance(part, dict)):
            raise ProviderError('模型拒绝了此请求，请调整描述', code='refusal')
        texts = [part.get('text', '') for part in content if isinstance(part, dict)
                 and part.get('type') in {'text', 'output_text'}]
        if any(not isinstance(text, str) for text in texts):
            raise ProviderError('模型文本项格式无效', code='invalid_response')
        return ''.join(texts)
    if content is None:
        return ''
    raise ProviderError('模型文本格式无效', code='invalid_response')


def _parse_response(body, protocol):
    if not isinstance(body, dict):
        raise ProviderError('模型响应格式无效', code='invalid_response')
    calls, text = [], ''
    if protocol == 'responses':
        status = body.get('status')
        if status != 'completed':
            raise ProviderError('模型回复未完成或被截断，未执行工具', code='incomplete_response')
        output = body.get('output')
        if not isinstance(output, list):
            raise ProviderError('模型响应缺少完整 output', code='invalid_response')
        for item in output:
            if not isinstance(item, dict):
                raise ProviderError('模型 output 项格式无效', code='invalid_response')
            if item.get('type') in {'message', 'function_call'} and 'status' in item and item['status'] != 'completed':
                raise ProviderError('模型 output 项未完成，未执行工具', code='incomplete_response')
            if item.get('type') == 'message':
                text += _text_content(item.get('content'))
            elif item.get('type') == 'function_call':
                calls.append({'id': item.get('call_id'), 'name': item.get('name'), 'arguments': item.get('arguments')})
        finish = status
    else:
        choices = body.get('choices')
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ProviderError('模型响应缺少完整 choices', code='invalid_response')
        choice = choices[0]
        finish = choice.get('finish_reason')
        if finish not in {'stop', 'tool_calls'}:
            raise ProviderError('模型回复被截断或未完成，未执行工具', code='incomplete_response')
        message = choice.get('message')
        if not isinstance(message, dict):
            raise ProviderError('模型消息格式无效', code='invalid_response')
        if message.get('refusal'):
            raise ProviderError('模型拒绝了此请求，请调整描述', code='refusal')
        text = _text_content(message.get('content'))
        tool_calls = message.get('tool_calls') or []
        if not isinstance(tool_calls, list):
            raise ProviderError('模型工具调用格式无效', code='invalid_tool_call')
        for call in tool_calls:
            if not isinstance(call, dict) or call.get('type') != 'function':
                raise ProviderError('模型请求了不支持的工具类型', code='invalid_tool_call')
            function = call.get('function')
            if not isinstance(function, dict):
                raise ProviderError('模型函数调用格式无效', code='invalid_tool_call')
            calls.append({'id': call.get('id'), 'name': function.get('name'), 'arguments': function.get('arguments')})
        output = None
    _validate_calls(calls)
    return {'text': text, 'tool_calls': calls, 'provider_output': output,
            'usage': _usage(body.get('usage'), protocol), 'response_id': body.get('id'), 'finish_reason': finish}


async def _emit(callback, text):
    if callback and text:
        result = callback(text)
        if inspect.isawaitable(result):
            await result


async def _sse_events(content):
    decoder = codecs.getincrementaldecoder('utf-8')()
    buffer, data, total = '', [], 0
    async for chunk in content.iter_chunked(8192):
        total += len(chunk)
        if total > MAX_BODY_BYTES:
            raise ProviderError('模型响应超过大小上限', code='invalid_response')
        try:
            buffer += decoder.decode(chunk)
        except UnicodeDecodeError:
            raise ProviderError('模型流返回无效 UTF-8', code='invalid_response') from None
        while '\n' in buffer:
            line, buffer = buffer.split('\n', 1)
            line = line.rstrip('\r')
            if not line:
                if data:
                    yield '\n'.join(data)
                    data = []
            elif line.startswith('data:'):
                value = line[5:]
                data.append(value[1:] if value.startswith(' ') else value)
    try:
        buffer += decoder.decode(b'', final=True)
    except UnicodeDecodeError:
        raise ProviderError('模型流在 UTF-8 字符中间中断', code='incomplete_response') from None
    # An unterminated final event is intentionally not consumed: transport completeness matters.


async def _stream(response, protocol, callback, secret):
    chat = {'choices': [{'message': {'role': 'assistant', 'content': '', 'tool_calls': []}, 'finish_reason': None}]}
    fragments, ended, terminal = {}, False, None
    async for raw in _sse_events(response.content):
        if raw == '[DONE]':
            ended = True
            break
        try:
            event = json.loads(raw, parse_constant=_reject_constant)
        except (ValueError, RecursionError):
            raise ProviderError('模型流包含无效 JSON', code='invalid_response') from None
        if not isinstance(event, dict):
            raise ProviderError('模型流事件格式无效', code='invalid_response')
        if 'error' in event or event.get('type') in {'error', 'response.failed', 'response.incomplete'}:
            raise ProviderError('模型流失败或回复不完整：' + _safe_error(
                event.get('message') or event.get('error') or event.get('type'), secret), code='incomplete_response')
        if protocol == 'responses':
            if event.get('type') == 'response.output_text.delta':
                await _emit(callback, event.get('delta', ''))
            elif event.get('type') == 'response.completed':
                terminal = event.get('response')
        else:
            if event.get('id'):
                chat['id'] = event['id']
            if isinstance(event.get('usage'), dict):
                chat['usage'] = event['usage']
            for choice in event.get('choices') or []:
                if choice.get('index', 0) != 0:
                    continue
                target = chat['choices'][0]
                delta = choice.get('delta') or {}
                if delta.get('refusal'):
                    raise ProviderError('模型拒绝了此请求，请调整描述', code='refusal')
                content = _text_content(delta.get('content'))
                target['message']['content'] += content
                await _emit(callback, content)
                for part in delta.get('tool_calls') or []:
                    index = part.get('index')
                    if type(index) is not int or index < 0:
                        raise ProviderError('工具参数流缺少索引', code='invalid_tool_call')
                    call = fragments.setdefault(index, {'id': '', 'type': 'function', 'function': {'name': '', 'arguments': ''}})
                    if part.get('id'):
                        call['id'] += part['id']
                    if part.get('type') not in {None, 'function'}:
                        raise ProviderError('模型请求了不支持的工具类型', code='invalid_tool_call')
                    function = part.get('function') or {}
                    for key in ('name', 'arguments'):
                        if function.get(key):
                            call['function'][key] += function[key]
                if choice.get('finish_reason') is not None:
                    target['finish_reason'] = choice['finish_reason']
    if protocol == 'responses':
        if terminal is None:
            raise ProviderError('模型流中断，未收到完整终态，未执行工具', code='incomplete_response')
        return _parse_response(terminal, protocol)
    if not ended:
        raise ProviderError('模型流中断，未收到完整结束标记，未执行工具', code='incomplete_response')
    chat['choices'][0]['message']['tool_calls'] = [fragments[key] for key in sorted(fragments)]
    return _parse_response(chat, protocol)


async def complete(http, config, history, tools, instructions, on_text=None):
    """Return only a complete, validated model turn; this module never executes tools."""
    endpoint = _endpoint(config)
    protocol = config.get('protocol', 'responses')
    key = str(config.get('api_key', '')).strip()
    if '\r' in key or '\n' in key:
        raise ProviderError('Agent API 密钥格式无效', code='invalid_config')
    if not config.get('model'):
        raise ProviderError('请配置 Agent 模型名称', code='invalid_config')
    payload = _payload(config, history, tools, instructions, on_text is not None)
    headers = {'Content-Type': 'application/json', 'Accept': 'text/event-stream' if on_text else 'application/json'}
    if key:
        headers['Authorization'] = 'Bearer ' + key
    timeout = aiohttp.ClientTimeout(total=float(config.get('timeout_seconds', 180)))
    for attempt in range(3):
        response_accepted = False
        try:
            async with http.post(endpoint, json=payload, headers=headers, timeout=timeout, allow_redirects=False) as response:
                if response.status >= 300:
                    body = (await response.content.read(8192)).decode(errors='replace')
                    try:
                        error = json.loads(body).get('error', {})
                        body = error.get('message', body) if isinstance(error, dict) else error
                    except (ValueError, AttributeError):
                        pass
                    retryable = response.status in TRANSIENT_STATUS
                    if retryable and attempt < 2:
                        try:
                            delay = max(0, min(30, float(response.headers.get('Retry-After', 2**attempt))))
                        except ValueError:
                            delay = 2**attempt
                        logging.warning('Agent 模型服务暂时不可用（HTTP %s），%s 秒后重试', response.status, delay)
                        await asyncio.sleep(delay)
                        continue
                    raise ProviderError(f'Agent API HTTP {response.status}：' + _safe_error(body, key),
                                        status=response.status, retryable=retryable)
                # WARN: A successful response may already be billed, even if its JSON body later disconnects.
                response_accepted = True
                if 'text/event-stream' in response.headers.get('Content-Type', '').lower():
                    return await _stream(response, protocol, on_text, key)
                body = bytearray()
                async for chunk in response.content.iter_chunked(8192):
                    body.extend(chunk)
                    if len(body) > MAX_BODY_BYTES:
                        raise ProviderError('模型响应超过大小上限', code='invalid_response')
                try:
                    raw = json.loads(body, parse_constant=_reject_constant)
                except (ValueError, UnicodeDecodeError, RecursionError):
                    raise ProviderError('模型未返回完整有效的 JSON 响应', code='invalid_response') from None
                result = _parse_response(raw, protocol)
                await _emit(on_text, result['text'])
                return result
        except (aiohttp.ClientError, TimeoutError, OSError) as exc:
            retryable = isinstance(exc, aiohttp.ClientConnectorError) and not response_accepted
            if attempt < 2 and retryable:
                logging.warning('Agent 模型连接暂时中断，正在重试（%s/2）', attempt + 1)
                await asyncio.sleep(2**attempt)
                continue
            raise ProviderError('Agent 模型连接中断或超时，请继续原会话；未执行不完整工具调用',
                                code='connection_error', retryable=retryable) from None
