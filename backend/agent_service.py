"""Durable sidebar conversations and validated Agent execution."""
import asyncio
import copy
import json
import os
import time
import uuid
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .agent_prompts import DIRECTOR, PROMPT_VERSION
from .agent_provider import complete
from .agent_tools import TOOLS, ToolApprovalRequired, ToolContext, execute_tool
from .agent_vision import visual_history
from .colab import clean

TERMINAL = {'completed', 'failed', 'cancelled', 'interrupted'}
DEFAULT_CONFIG = {'base_url': '', 'model': '', 'api_key': '', 'protocol': 'responses',
                  'timeout_seconds': 240, 'max_output_tokens': 12000, 'strict_tools': False,
                  'cache_key': '', 'cache_options': {}}


class Body(BaseModel):
    model_config = ConfigDict(extra='forbid')


class ProviderSettings(Body):
    base_url: str = Field(max_length=2048)
    model: str = Field(min_length=1, max_length=200)
    protocol: Literal['responses', 'chat_completions'] = 'responses'
    api_key: str | None = Field(default=None, max_length=4096)
    timeout_seconds: int = Field(default=240, ge=15, le=600)
    max_output_tokens: int = Field(default=12000, ge=1024, le=64000)
    strict_tools: bool = False
    cache_key: str = Field(default='', max_length=64)
    cache_options: dict = Field(default_factory=dict)


class NewSession(Body):
    project_id: str
    canvas_id: str


class Message(Body):
    message: str = Field(min_length=1, max_length=20000)
    mode: Literal['plan', 'auto'] = 'plan'
    account: str
    selected_nodes: list[str] = Field(default_factory=list, max_length=100)
    request_id: str = Field(min_length=1, max_length=100)


class Approval(Body):
    approved: bool
    call_id: str = Field(min_length=1, max_length=200)


class AgentService:
    def __init__(self, manager):
        self.manager, self.store = manager, manager.store
        self.tasks, self.approvals = {}, {}
        self.config_path = self.store.directory / 'agent-private.json'
        for run in self.store.all('agent_runs'):
            if run['status'] not in TERMINAL:
                self.store.patch('agent_runs', run['id'], status='interrupted', message='本地服务已重启，可继续原对话；已提交的生成不会重复执行')

    def config(self):
        try:
            config = {**DEFAULT_CONFIG, **json.loads(self.config_path.read_text(encoding='utf-8'))}
            config['strict_tools'] = False
            return config
        except FileNotFoundError:
            return DEFAULT_CONFIG.copy()

    def public_config(self):
        config = self.config()
        return {k: v for k, v in config.items() if k != 'api_key'} | {
            'has_key': bool(config.get('api_key')), 'configured': bool(config.get('base_url') and config.get('model') and config.get('api_key'))}

    def save_config(self, body):
        if body.strict_tools:
            raise ValueError('当前画布工具使用可扩展参数，仅支持非严格协议模式；所有工具仍由本地完整校验')
        config = self.config()
        values = body.model_dump(exclude={'api_key'})
        endpoint = urlsplit(values['base_url'].strip().rstrip('/'))
        if (endpoint.scheme not in {'http', 'https'} or not endpoint.hostname or endpoint.username or endpoint.password
                or endpoint.query or endpoint.fragment):
            raise ValueError('模型地址需要 http(s) URL，不能包含凭据、查询参数或片段')
        if endpoint.scheme == 'http' and endpoint.hostname not in {'127.0.0.1', 'localhost', '::1'}:
            raise ValueError('非本机模型服务需要 HTTPS')
        if len(json.dumps(values['cache_options'])) > 4096:
            raise ValueError('缓存选项过长')
        values['base_url'] = values['base_url'].strip().rstrip('/')
        config.update(values)
        if body.api_key is not None and body.api_key.strip():
            config['api_key'] = body.api_key.strip()
        temporary = self.config_path.with_suffix('.tmp')
        with temporary.open('w', encoding='utf-8') as stream:
            os.chmod(temporary, 0o600)
            json.dump(config, stream, ensure_ascii=False)
        temporary.replace(self.config_path)
        return self.public_config()

    def error(self, exc):
        message = str(getattr(exc, 'detail', exc))
        key = self.config().get('api_key')
        if key:
            message = message.replace(key, '[redacted]')
        return clean(message)[-2500:]

    def project(self, key, canvas=None):
        project = self.store.get('projects', key)
        if not project or project.get('deleted_at'):
            raise HTTPException(404, '项目不存在或已移入回收站')
        if canvas and not any(c['id'] == canvas for c in project.get('canvases', [])):
            raise HTTPException(404, '画布不存在')
        return project

    def session(self, key):
        session = self.store.get('agent_sessions', key)
        if not session:
            raise HTTPException(404, '对话不存在')
        self.project(session['project_id'], session['canvas_id'])
        return session

    def run(self, key):
        run = self.store.get('agent_runs', key)
        if not run:
            raise HTTPException(404, 'Agent 任务不存在')
        return run

    def public_session(self, key):
        session = self.session(key)
        runs = sorted((r for r in self.store.all('agent_runs') if r['session_id'] == key), key=lambda r: r['created_at'])
        return {k: v for k, v in session.items() if k not in {'history', 'protocol', 'model', 'checkpoints'}} | {
            'runs': runs, 'messages': session.get('messages', [])}

    def create_session(self, project, canvas):
        self.project(project, canvas)
        key = uuid.uuid4().hex
        return self.store.put('agent_sessions', key, {'id': key, 'project_id': project, 'canvas_id': canvas,
            'title': '新对话', 'created_at': time.time(), 'updated_at': time.time(), 'history': [], 'messages': [], 'active_run_id': None})

    def start(self, key, body):
        session, config = self.session(key), self.config()
        if not body.message.strip():
            raise ValueError('消息不能为空')
        if not self.public_config()['configured']:
            raise ValueError('请先配置 Agent 的模型地址、模型名和 API Key')
        self.manager.machine(body.account)
        run_id = uuid.uuid5(uuid.NAMESPACE_URL, f'canvas-agent:{key}:{body.request_id}').hex
        previous = self.store.get('agent_runs', run_id)
        if previous:
            if (previous['user_message'] != body.message or previous['account'] != body.account
                    or previous['mode'] != body.mode or previous.get('selected_nodes', []) != body.selected_nodes):
                raise HTTPException(409, '相同请求 ID 不能用于不同消息或执行上下文')
            return previous
        active = session.get('active_run_id')
        task = self.tasks.get(active)
        if active and (self.run(active)['status'] not in TERMINAL or task and not task.done()):
            raise HTTPException(409, '当前对话仍在执行，请先停止或等待完成')
        if self.pending_calls(session['history']):
            raise HTTPException(409, '当前对话有未完成的工具调用，请继续原任务或新建对话')
        if ((session.get('protocol') and session['protocol'] != config['protocol'])
                or (session.get('model') and session['model'] != config['model'])):
            raise ValueError('更换模型或协议后请新建对话，以保留完整工具调用历史')
        project = self.project(session['project_id'], session['canvas_id'])
        canvas = next(c for c in project['canvases'] if c['id'] == session['canvas_id'])
        available = {n['id'] for n in canvas['nodes']}
        if not set(body.selected_nodes) <= available:
            raise ValueError('所选节点已变化，请重新选择')
        context = {'project_id': session['project_id'], 'canvas_id': session['canvas_id'],
                   'mode': body.mode, 'account': body.account, 'selected_nodes': body.selected_nodes}
        history_message = body.message.strip() + '\n\n当前请求上下文：' + json.dumps(context, ensure_ascii=False, sort_keys=True)
        session['history'].append({'role': 'user', 'content': history_message})
        session['messages'].append({'role': 'user', 'content': body.message.strip()})
        session.update(active_run_id=run_id, last_run_id=run_id, protocol=config['protocol'], model=config['model'], updated_at=time.time())
        if session['title'] == '新对话':
            session['title'] = body.message.strip()[:32]
        self.store.put('agent_sessions', key, session)
        run = self.store.put('agent_runs', run_id, {'id': run_id, 'session_id': key, 'status': 'running',
            'message': '正在理解创意', 'user_message': body.message, 'mode': body.mode, 'account': body.account,
            'selected_nodes': body.selected_nodes,
            'events': [], 'usage': {'input_tokens': 0, 'output_tokens': 0, 'cached_tokens': None},
            'created_at': time.time(), 'updated_at': time.time(), 'project_revision': project['revision'],
            'prompt_version': PROMPT_VERSION, 'model': config['model'], 'protocol': config['protocol']})
        self.tasks[run_id] = asyncio.create_task(self.execute(run_id))
        return run

    def update(self, key, **changes):
        return self.store.patch('agent_runs', key, **changes)

    def event(self, key, event):
        run = self.run(key)
        previous = next((e for e in run['events'] if e['id'] == event['id']), None)
        if previous:
            previous.update(event)
        else:
            run['events'].append({'created_at': time.time(), **event})
        return self.update(key, events=run['events'])

    def save_history(self, key, history, text=None):
        session = self.session(key)
        session['history'] = history
        if text:
            session['messages'].append({'role': 'assistant', 'content': text})
        return self.store.patch('agent_sessions', key, **{k: v for k, v in session.items() if k != 'id'})

    def record_usage(self, key, new_usage):
        usage = self.run(key)['usage']
        for field in ('input_tokens', 'output_tokens'):
            usage[field] = (usage.get(field) or 0) + (new_usage.get(field) or 0)
        cached = new_usage.get('cached_tokens')
        if cached is not None:
            usage['cached_tokens'] = (usage.get('cached_tokens') or 0) + cached
        self.update(key, usage=usage)

    async def compact(self, session, history, config):
        if len(json.dumps(history, ensure_ascii=False)) < 240000:
            return history
        if self.pending_calls(history):
            return history
        pending, boundaries = set(), []
        for index, message in enumerate(history):
            if message['role'] == 'assistant':
                pending.update(call['id'] for call in message.get('tool_calls', []))
            elif message['role'] == 'tool':
                pending.discard(message.get('tool_call_id'))
            if not pending and index + 1 < len(history):
                boundaries.append(index + 1)
        if not boundaries:
            return history
        target = max(1, len(history) - 8)
        cut = min(boundaries, key=lambda boundary: (abs(boundary - target), boundary))
        summary = await complete(self.manager.http, config, [{'role': 'user', 'content': json.dumps(history[:cut], ensure_ascii=False)}], [],
            '将创作对话压缩为可继续执行的事实检查点。保留用户目标、故事/人物视觉契约、所有实际node/asset/job ID、已完成与待做项、错误及未决授权。不要编造，不输出工具调用。',
            on_text=lambda _: None)
        current = self.session(session['id'])
        if current.get('active_run_id'):
            self.record_usage(current['active_run_id'], summary.get('usage') or {})
        if not summary.get('text') or summary.get('tool_calls'):
            raise RuntimeError('长对话检查点为空，停止以保留完整历史')
        checkpoint = {'role': 'user', 'content': '已保存的长任务检查点：\n' + summary['text']}
        self.store.patch('agent_sessions', session['id'], checkpoints=[*current.get('checkpoints', []),
            {'created_at': time.time(), 'history': copy.deepcopy(history), 'summary': summary['text'],
             'model': config['model'], 'protocol': config['protocol']}])
        return [checkpoint, *history[cut:]]

    async def execute(self, key):
        run = self.run(key)
        session = self.session(run['session_id'])
        history = copy.deepcopy(session['history'])
        config = self.config()
        try:
            if config['protocol'] != run['protocol'] or config['model'] != run['model']:
                raise ValueError('模型设置已改变，请恢复原设置或新建对话')
            if history and history[-1]['role'] == 'assistant' and not history[-1].get('tool_calls') and not self.pending_calls(history):
                self.update(key, status='completed', message='已完成', approval=None)
                return
            for round_number in range(80):
                pending = self.pending_calls(history)
                if not pending:
                    history = await self.compact(session, history, config)
                    self.save_history(session['id'], history)
                    text, last_save = '', 0
                    event_id = f'model-{len(history)}'
                    async def on_text(delta):
                        nonlocal text, last_save
                        text += delta
                        if time.monotonic() - last_save > .25:
                            self.event(key, {'id': event_id, 'type': 'message', 'status': 'running', 'text': text})
                            last_save = time.monotonic()
                    self.update(key, status='running', message='正在设计与编排')
                    turn = await complete(self.manager.http, config, await visual_history(self.store, history), TOOLS, DIRECTOR, on_text=on_text)
                    self.record_usage(key, turn.get('usage') or {})
                    response = {'role': 'assistant', 'content': turn.get('text') or '', 'tool_calls': turn.get('tool_calls') or []}
                    if turn.get('provider_output') is not None:
                        response['provider_output'] = turn['provider_output']
                    history.append(response)
                    self.save_history(session['id'], history, response['content'])
                    self.event(key, {'id': event_id, 'type': 'message', 'status': 'completed', 'text': response['content']})
                    pending = response['tool_calls']
                    if not pending:
                        self.update(key, status='completed', message='已完成', approval=None)
                        return
                for call in pending:
                    call_id = call['id']
                    ledger_id = uuid.uuid5(uuid.NAMESPACE_URL, f'agent-call:{session["id"]}:{call_id}').hex
                    ledger = self.store.get('agent_calls', ledger_id)
                    try:
                        args = json.loads(call['arguments'])
                        if not isinstance(args, dict):
                            raise ValueError('工具参数需要 JSON 对象')
                        encoded = json.dumps(args, sort_keys=True, allow_nan=False)
                    except (ValueError, TypeError) as exc:
                        result = {'error': '工具参数无效：' + str(exc), 'retryable': True}
                    else:
                        if ledger and (ledger['name'] != call['name'] or ledger['run_id'] != key
                                       or json.dumps(ledger['arguments'], sort_keys=True, allow_nan=False) != encoded):
                            raise RuntimeError('工具调用 ID 与已保存的名称、参数或任务不一致，已停止冲突调用')
                        result = await self.call_tool(run, session, call, args, ledger_id, ledger)
                    self.event(key, {'id': call_id, 'status': 'failed' if 'error' in result else 'completed', 'result': result})
                    history.append({'role': 'tool', 'tool_call_id': call_id, 'name': call['name'], 'content': json.dumps(result, ensure_ascii=False, separators=(',', ':'))})
                    self.save_history(session['id'], history)
                    if result.get('uncertain'):
                        raise RuntimeError(result['error'])
                    project = self.project(session['project_id'])
                    self.update(key, project_revision=project['revision'])
                await asyncio.sleep(0)
            raise RuntimeError('已达到单轮 80 次模型调用限制，已有成果已保存；可继续对话')
        except asyncio.CancelledError:
            if self.run(key)['status'] not in TERMINAL:
                self.update(key, status='interrupted', message='Agent 已暂停，已提交生成与成果保留')
        except Exception as exc:
            self.update(key, status='failed', error=self.error(exc), message=self.error(exc))
        finally:
            self.approvals.pop(key, None)
            session = self.store.get('agent_sessions', run['session_id'])
            if session and session.get('active_run_id') == key and self.run(key)['status'] in TERMINAL:
                self.store.patch('agent_sessions', session['id'], active_run_id=None)

    @staticmethod
    def pending_calls(history):
        pending = {}
        for message in history:
            if message['role'] == 'assistant':
                for call in message.get('tool_calls', []):
                    pending[call['id']] = call
            elif message['role'] == 'tool':
                pending.pop(message.get('tool_call_id'), None)
        return list(pending.values())

    async def invoke_tool(self, name, args, ctx):
        if ctx.mode == 'plan' and (name in {'generate_nodes', 'resume_job', 'extract_frame', 'assemble_film'}
                                   or name == 'server' and args.get('action') in {'connect', 'stop'}):
            raise ValueError('当前是策划模式，不能生成、处理媒体或连接及关闭 GPU；切换自动模式后继续')
        if name != 'get_jobs' or not args.get('wait', True):
            return await execute_tool(name, args, ctx)
        # NOTE: GPU waits stay local so elapsed time never creates paid model polling.
        probe = {**args, 'wait': False, 'wait_seconds': 0}
        previous, resumed = None, set()
        self.update(ctx.run_id, status='waiting', message='等待生成任务完成')
        while True:
            result = await execute_tool(name, probe, ctx)
            if result.get('all_terminal') or 'error' in result:
                return result
            for job in result.get('jobs', []):
                if job.get('status') != 'recovering':
                    continue
                worker = self.manager.workers.get(ctx.account)
                if worker and not worker.done():
                    continue
                if ctx.mode != 'auto':
                    return {**result, 'error': '原生成等待恢复；请切换自动模式继续或在服务器面板恢复任务', 'retryable': True}
                if job['id'] in resumed:
                    return {**result, 'error': '原任务恢复受阻：' + self.manager.machine(ctx.account).get('message', '请检查服务器状态'), 'retryable': True}
                # WARN: Resume the durable job; never create a replacement generation while waiting.
                self.manager.resume(job['id'])
                resumed.add(job['id'])
            signature = json.dumps(result, ensure_ascii=False, sort_keys=True)
            if signature != previous:
                self.event(ctx.run_id, {'id': ctx.call_id, 'status': 'running', 'result': result})
                previous = signature
            await asyncio.sleep(.5)

    async def approve_tool(self, run, call, args, ledger_id):
        ledger = self.store.get('agent_calls', ledger_id)
        if ledger.get('approval_decision') is not None:
            return ledger['approval_decision']
        future = asyncio.get_running_loop().create_future()
        self.approvals[run['id']] = future
        self.store.patch('agent_calls', ledger_id, status='awaiting_approval')
        self.event(run['id'], {'id': call['id'], 'status': 'approval'})
        self.update(run['id'], status='awaiting_approval', message='此工具需要确认',
                    approval={'call_id': call['id'], 'name': call['name'], 'arguments': args})
        approved = await future
        self.approvals.pop(run['id'], None)
        self.update(run['id'], status='running', approval=None)
        return approved

    async def call_tool(self, run, session, call, args, ledger_id, ledger):
        key, call_id, name = run['id'], call['id'], call['name']
        if ledger and ledger['status'] == 'completed':
            return ledger['result']
        replay_safe = (name in {'inspect_canvas', 'inspect_assets', 'get_jobs', 'extract_frame', 'assemble_film', 'cancel_job'}
                       or name == 'server' and args.get('action') in {'status', 'logs'}
                       or name == 'manage_project' and args.get('action') in {'create_canvas', 'duplicate_canvas'})
        if ledger and ledger['status'] == 'started' and not replay_safe:
            result = self.reconcile(run, call_id) if name == 'generate_nodes' else None
            if result is None:
                result = {'error': '工具执行结果不确定，已停止自动重放；请检查已有节点和任务。调用：' + name, 'uncertain': True}
            self.store.patch('agent_calls', ledger_id, status='completed', result=result)
            return result
        self.event(key, {'id': call_id, 'type': 'tool', 'name': name, 'arguments': args, 'status': 'running'})
        self.update(key, message='执行 ' + name)
        if not ledger:
            ledger = self.store.put('agent_calls', ledger_id, {'id': ledger_id, 'run_id': key, 'call_id': call_id,
                'name': name, 'status': 'started', 'arguments': args, 'created_at': time.time()})
        ctx = ToolContext(self.manager, session['project_id'], session['canvas_id'], run['account'], key, call_id, mode=run['mode'])
        try:
            if ledger['status'] == 'awaiting_approval':
                approved = await self.approve_tool(run, call, args, ledger_id)
                if not approved:
                    result = {'error': '用户拒绝此次操作，保留原状态'}
                    self.store.patch('agent_calls', ledger_id, status='completed', result=result)
                    return result
                ctx.approved = True
            self.store.patch('agent_calls', ledger_id, status='started')
            try:
                result = await self.invoke_tool(name, args, ctx)
            except ToolApprovalRequired:
                if ctx.approved:
                    raise RuntimeError('工具在明确授权后仍请求授权，已停止执行') from None
                approved = await self.approve_tool(run, call, args, ledger_id)
                if approved:
                    ctx.approved = True
                    self.store.patch('agent_calls', ledger_id, status='started')
                    result = await self.invoke_tool(name, args, ctx)
                else:
                    result = {'error': '用户拒绝此次操作，保留原状态'}
        except (ValueError, HTTPException, RuntimeError, OSError) as exc:
            result = {'error': self.error(exc), 'retryable': isinstance(exc, HTTPException) and exc.status_code == 409}
        self.store.patch('agent_calls', ledger_id, status='completed', result=result)
        return result

    def reconcile(self, run, call_id):
        jobs = [j for j in self.store.all('jobs') if j.get('creative', {}).get('agent_run_id') == run['id']
                and j.get('creative', {}).get('agent_call_id') == call_id]
        return {'job_ids': [j['id'] for j in jobs], 'recovered': True} if jobs else None

    async def cancel(self, key):
        run = self.run(key)
        if run['status'] not in TERMINAL:
            self.update(key, status='cancelled', message='Agent 已停止；已提交的生成任务继续保留')
            task = self.tasks.get(key)
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        return self.run(key)

    def resume(self, key):
        run, config = self.run(key), self.config()
        if run['status'] not in {'failed', 'interrupted', 'cancelled'}:
            raise ValueError('只有暂停或失败的 Agent 任务可以继续')
        task = self.tasks.get(key)
        if task and not task.done():
            raise HTTPException(409, '原任务仍在清理，请稍后继续')
        if config['protocol'] != run['protocol'] or config['model'] != run['model']:
            raise ValueError('继续前请恢复原模型与协议设置')
        session = self.session(run['session_id'])
        latest = session.get('last_run_id')
        if latest is None:
            runs = [item for item in self.store.all('agent_runs') if item['session_id'] == session['id']]
            latest = max(runs, key=lambda item: item['created_at'])['id'] if runs else key
        if latest != key:
            raise HTTPException(409, '原任务之后已有新对话消息，请继续最新任务或新建对话')
        active = session.get('active_run_id')
        if active and active != key and self.run(active)['status'] not in TERMINAL:
            raise HTTPException(409, '当前对话已有另一个执行任务')
        self.store.patch('agent_sessions', session['id'], active_run_id=key)
        self.update(key, status='running', error=None, message='继续原编排')
        self.tasks[key] = asyncio.create_task(self.execute(key))
        return self.run(key)

    async def close(self):
        for key, task in self.tasks.items():
            if not task.done():
                self.update(key, status='interrupted', message='本地服务退出，成果和任务已保存，可继续原编排')
                task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)


router = APIRouter(prefix='/api/agent')


def service(request):
    return request.app.state.agent


@router.get('/settings')
async def settings(request: Request):
    return service(request).public_config()


@router.put('/settings')
async def put_settings(body: ProviderSettings, request: Request):
    return service(request).save_config(body)


@router.post('/test')
async def test_provider(request: Request):
    agent = service(request)
    if not agent.public_config()['configured']:
        raise ValueError('请先保存模型地址、模型名和 API Key')
    try:
        result = await complete(agent.manager.http, agent.config(), [{'role': 'user', 'content': '用一句简短中文确认连接正常。'}], [], DIRECTOR,
                                on_text=lambda _: None)
        return {'text': result['text'], 'usage': result['usage']}
    except Exception as exc:
        raise HTTPException(502, agent.error(exc)) from exc


@router.get('/sessions')
async def list_sessions(request: Request, project_id: str, canvas_id: str | None = None):
    agent = service(request)
    agent.project(project_id, canvas_id)
    result = []
    for session in agent.store.all('agent_sessions'):
        if session['project_id'] == project_id and (not canvas_id or session['canvas_id'] == canvas_id):
            run = agent.store.get('agent_runs', session.get('active_run_id')) if session.get('active_run_id') else None
            result.append({k: session[k] for k in ('id', 'title', 'updated_at', 'canvas_id')} | {'status': run['status'] if run else 'completed'})
    return sorted(result, key=lambda item: item['updated_at'], reverse=True)


@router.post('/sessions', status_code=201)
async def new_session(body: NewSession, request: Request):
    session = service(request).create_session(body.project_id, body.canvas_id)
    return service(request).public_session(session['id'])


@router.get('/sessions/{key}')
async def read_session(key: str, request: Request):
    return service(request).public_session(key)


@router.post('/sessions/{key}/messages', status_code=202)
async def send_message(key: str, body: Message, request: Request):
    return service(request).start(key, body)


@router.get('/runs/{key}')
async def read_run(key: str, request: Request):
    return service(request).run(key)


@router.post('/runs/{key}/cancel')
async def cancel_run(key: str, request: Request):
    return await service(request).cancel(key)


@router.post('/runs/{key}/resume', status_code=202)
async def resume_run(key: str, request: Request):
    return service(request).resume(key)


@router.post('/runs/{key}/approval')
async def approve_run(key: str, body: Approval, request: Request):
    agent = service(request)
    agent.run(key)
    future = agent.approvals.get(key)
    if future is None or future.done():
        raise ValueError('此操作已处理或任务已中断，请继续原任务后再确认')
    run = agent.run(key)
    call_id = (run.get('approval') or {}).get('call_id')
    if not call_id:
        raise ValueError('当前任务没有待确认的工具调用')
    if body.call_id != call_id:
        raise HTTPException(409, '此确认对应的工具已变化，请检查当前操作')
    ledger_id = uuid.uuid5(uuid.NAMESPACE_URL, f'agent-call:{run["session_id"]}:{call_id}').hex
    ledger = agent.store.get('agent_calls', ledger_id)
    if not ledger or ledger['status'] != 'awaiting_approval' or ledger.get('approval_decision') is not None:
        raise ValueError('此次操作已处理，请刷新任务状态')
    agent.store.patch('agent_calls', ledger_id, approval_decision=body.approved)
    future.set_result(body.approved)
    return agent.run(key)
