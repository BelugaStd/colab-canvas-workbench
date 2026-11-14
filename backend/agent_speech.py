"""Durable private-channel Qwen dubbing over an existing owned Colab session."""
import asyncio
import base64
import hashlib
import json
import re
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

from . import agent_media, speech_remote
from .assets import asset_path

POLL_SECONDS = 5
WAIT_SECONDS = 7200
PREFIX = 'WB_SPEECH:'
UNCONFIRMED = {'pending', 'running', 'downloading', 'interrupted'}


def _script(request, bootstrap=False, offset=None):
    start = offset or 0
    encoded = base64.b64encode(json.dumps(request, ensure_ascii=False).encode()).decode()
    source = ('import base64,json,os,subprocess,sys,time\nfrom pathlib import Path\n'
              f'REQUEST_B64 = {encoded!r}\n'
              'request=json.loads(base64.b64decode(REQUEST_B64))\n'
              f'root=Path({speech_remote.REMOTE!r})\n'
              "task=root/request['id']\nstatus=task/'status.json'\n"
              'def emit():\n'
              " value=json.loads(status.read_text(encoding='utf-8')) if status.exists() else "
              "{'id':request['id'],'fingerprint':request['fingerprint'],'status':'interrupted','phase':'missing','utterances':[]}\n"
              " items=value.pop('utterances',[])\n value['completed_count']=len(items)\n"
              f" value['utterances']=items[{start}:{start + 8}] if {offset is not None!r} else []\n"
              f" print({PREFIX!r}+json.dumps(value,ensure_ascii=False))\n")
    if not bootstrap:
        return source + 'emit()\n'
    worker = Path(speech_remote.__file__).read_bytes()
    encoded_worker = base64.b64encode(worker).decode()
    worker_name = 'runner-' + hashlib.sha256(worker).hexdigest()[:16] + '.py'
    return source + (
        'import fcntl\n'
        'task.mkdir(parents=True,exist_ok=True)\n'
        "saved=task/'request.json'\n"
        "if saved.exists() and json.loads(saved.read_text(encoding='utf-8'))!=request:\n"
        " raise ValueError('speech_result_id_conflict')\n"
        "lock=(task/'.run.lock').open('a')\n"
        'try:\n fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)\n'
        'except BlockingIOError:\n emit()\n'
        'else:\n'
        " old=json.loads(status.read_text(encoding='utf-8')) if status.exists() else {}\n"
        " if old.get('status') not in ('completed','failed'):\n"
        "  saved.write_text(json.dumps(request,ensure_ascii=False),encoding='utf-8')\n"
        f'  runner=root/{worker_name!r}\n'
        "  staging=root/(runner.name+'.'+str(os.getpid())+'.part')\n"
        f'  staging.write_bytes(base64.b64decode({encoded_worker!r}))\n'
        '  staging.replace(runner)\n'
        "  value={'id':request['id'],'fingerprint':request['fingerprint'],'status':'running','phase':'queued',"
        "'started_at':old.get('started_at',time.time()),'utterances':[]}\n"
        "  temporary=task/'status.json.part'\n"
        "  temporary.write_text(json.dumps(value),encoding='utf-8')\n  temporary.replace(status)\n"
        "  with (task/'worker.log').open('ab') as log:\n"
        "   subprocess.Popen([sys.executable,str(runner),'--run',str(saved)],stdin=subprocess.DEVNULL,"
        "stdout=log,stderr=subprocess.STDOUT,start_new_session=True,pass_fds=(lock.fileno(),),"
        "env={**os.environ,'WB_SPEECH_LOCK_FD':str(lock.fileno())})\n"
        ' emit()\n')


async def _execute(ctx, request, bootstrap=False, offset=None):
    folder = ctx.manager.store.directory / 'agent-speech' / request['id']
    folder.mkdir(parents=True, exist_ok=True)
    script = folder / ('bootstrap.py' if bootstrap else f'query-{offset}.py')
    script.write_text(_script(request, bootstrap, offset), encoding='utf-8')
    task = ctx.manager.store.get('agent_speech_tasks', request['id'])
    try:
        output = await ctx.manager.colab.execute(ctx.account, task['session'], script)
    except asyncio.CancelledError:
        raise
    except Exception:
        raise ConnectionError('配音私有通道暂时不可用；原任务已保留，继续相同请求可查询恢复') from None
    lines = [line[len(PREFIX):] for line in output.splitlines() if line.startswith(PREFIX)]
    if len(lines) != 1:
        raise RuntimeError('配音状态响应不完整；原任务保留，不能确认已完成')
    try:
        result = json.loads(lines[0])
    except ValueError:
        raise RuntimeError('配音状态不是有效 JSON') from None
    if (not isinstance(result, dict) or result.get('id') != request['id']
            or result.get('fingerprint') != request['fingerprint']
            or result.get('status') not in {'running', 'completed', 'failed', 'interrupted'}):
        raise RuntimeError('配音状态与当前任务不匹配')
    return result


def _ready(ctx, previous=None):
    if ctx.mode != 'auto':
        raise ValueError('规划模式不能执行配音，请使用自动模式')
    machine = ctx.manager.machine(ctx.account)
    install = ctx.manager.store.get('meta', 'install', {}).get('id')
    if (not machine or machine.get('phase') != 'ready' or not machine.get('session') or not install
            or not machine['session'].startswith('wb-' + install + '-')):
        raise RuntimeError('配音需要已就绪的原 owned GPU 会话；不会自动申请或重启服务器')
    if previous and previous['session'] != machine['session']:
        raise RuntimeError('配音原会话已改变，不能在新 GPU 上静默重提交')
    if ctx.manager.active_jobs(ctx.account):
        raise RuntimeError('GPU 仍有生成任务，请等待完成后再配音')
    if any(task.get('account') == ctx.account and task.get('status') in UNCONFIRMED
           and task['id'] != (previous or {}).get('id')
           for task in ctx.manager.store.all('agent_speech_tasks')):
        raise RuntimeError('此账号仍有未确认结束的配音，请使用原任务 ID 查询或恢复，不能绕过 GPU 占用')
    return machine


async def refresh_pending(manager, account):
    """Observe original remote tasks without deploying, synthesizing or downloading."""
    store, summaries = manager.store, []
    for task in store.all('agent_speech_tasks'):
        if task.get('account') != account or task.get('status') not in UNCONFIRMED:
            continue
        summary = {key: task[key] for key in ('id', 'status', 'phase', 'completed_count') if key in task}
        machine = manager.machine(account) or {}
        install = store.get('meta', 'install', {}).get('id')
        if (not install or not machine.get('session') or task.get('session') != machine['session']
                or not machine['session'].startswith('wb-' + install + '-')):
            summary['message'] = '原 owned 配音会话暂不可查询；保留未确认占用'
            summaries.append(summary)
            continue
        try:
            request = task['request']
            speech_remote.make_request(task['id'], request['utterances'])
            if (request.get('id') != task['id'] or request.get('fingerprint') != task['fingerprint']
                    or speech_remote.digest({k: v for k, v in request.items() if k not in {'id', 'fingerprint'}})
                    != task['fingerprint']):
                raise ValueError('stored_speech_request_changed')
            state = await _execute(SimpleNamespace(manager=manager, account=account), request)
            current = store.get('agent_speech_tasks', task['id'])
            if current.get('status') not in UNCONFIRMED:
                summaries.append({key: current[key] for key in ('id', 'status', 'phase', 'completed_count') if key in current})
                continue
            if (current.get('fingerprint') != task['fingerprint'] or current.get('session') != task['session']
                    or (manager.machine(account) or {}).get('session') != task['session']):
                raise RuntimeError('speech_session_changed_during_query')
            status = {'completed': 'generated', 'failed': 'failed', 'running': 'running',
                      'interrupted': 'interrupted'}[state['status']]
            count = state.get('completed_count', 0)
            if type(count) is not int or not 0 <= count <= len(request['utterances']):
                raise ValueError('invalid_speech_completion_count')
            # WARN: Remote completion only frees GPU occupancy; local assets may still need downloading and registration.
            updated = store.patch('agent_speech_tasks', task['id'], status=status,
                                  phase=state.get('phase'), completed_count=count)
            summary = {key: updated[key] for key in ('id', 'status', 'phase', 'completed_count')}
        except asyncio.CancelledError:
            raise
        except Exception:
            summary['message'] = '配音状态暂不可确认；保留原任务及未确认占用'
        summaries.append(summary)
    return summaries


def _validate_outputs(request, outputs):
    if not isinstance(outputs, list) or len(outputs) != len(request['utterances']):
        raise RuntimeError('配音返回的台词数量不匹配')
    for expected, output in zip(request['utterances'], outputs):
        if not isinstance(output, dict) or any(output.get(k) != v for k, v in expected.items()):
            raise RuntimeError('配音声线、台词或表演指令与原文不匹配')
        if (output.get('cache_key') != speech_remote.cache_key(expected)
                or not isinstance(output.get('sha256'), str) or not re.fullmatch('[0-9a-f]{64}', output['sha256'])
                or type(output.get('samples')) is not int or type(output.get('sample_rate')) is not int
                or not 8000 <= output['sample_rate'] <= 96000
                or not 0 < output['samples'] <= output['sample_rate'] * 120
                or output.get('duration') != output['samples'] / output['sample_rate']
                or type(output.get('cache_hit')) is not bool):
            raise RuntimeError('配音音频元数据不完整或越界')


async def _register(ctx, request, outputs):
    _validate_outputs(request, outputs)
    folder = ctx.manager.store.directory / 'agent-speech' / request['id']
    utterances, assets = [], []
    for item in outputs:
        asset_id = uuid.uuid5(uuid.UUID(request['id']), item['id']).hex
        metadata = {'result_id': request['id'], 'utterance_id': item['id'],
                    'model': speech_remote.REPOSITORY, 'model_revision': speech_remote.REVISION,
                    'sdk_version': speech_remote.SDK_VERSION, 'language': 'Chinese',
                    'fingerprint': request['fingerprint'],
                    **{k: item[k] for k in ('text', 'speaker', 'instruct', 'samples', 'sample_rate', 'cache_key', 'sha256')}}
        existing = ctx.manager.store.get('assets', asset_id)
        if existing:
            existing_path, _ = asset_path(ctx.manager.store, asset_id)
            if existing.get('agent_speech') != metadata:
                raise RuntimeError('配音素材 ID 已绑定其它原文或声线')
            if hashlib.sha256(existing_path.read_bytes()).hexdigest() != item['sha256']:
                raise RuntimeError('已注册配音文件损坏，不能作为有效缓存复用')
            asset = existing
        else:
            path = folder / (item['cache_key'] + '.wav')
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != item['sha256']:
                remote = speech_remote.REMOTE.lstrip('/') + '/cache/' + item['cache_key'] + '.wav'
                try:
                    await ctx.manager.colab.download(ctx.account,
                        ctx.manager.store.get('agent_speech_tasks', request['id'])['session'], remote, path)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    raise ConnectionError('配音音频下载中断；原合成已保留，继续相同请求可恢复下载') from None
            actual = await asyncio.to_thread(speech_remote.inspect_wave, path)
            if any(actual[k] != item[k] for k in ('samples', 'sample_rate', 'duration', 'sha256')):
                raise RuntimeError('配音私有下载的音频长度或校验和不匹配')
            asset = await agent_media.register(ctx.manager.store, path, item['id'] + ' · ' + item['speaker'],
                {'agent_speech': metadata, 'agent_project_id': ctx.project_id, 'agent_canvas_id': ctx.canvas_id}, asset_id)
        assets.append(asset)
        utterances.append({**{k: item[k] for k in ('id', 'speaker', 'text', 'instruct', 'duration', 'samples',
                                                   'sample_rate', 'cache_hit')}, 'asset_id': asset_id})
    return {'id': request['id'], 'status': 'completed', 'engine': 'qwen3-tts',
            'model': speech_remote.REPOSITORY, 'model_revision': speech_remote.REVISION,
            'utterances': utterances, 'assets': assets, 'lip_sync': False}


async def _synthesize(ctx, request):
    store = ctx.manager.store
    previous = store.get('agent_speech_tasks', request['id'])
    if previous:
        if (previous.get('fingerprint') != request['fingerprint'] or previous.get('account') != ctx.account
                or previous.get('project_id') != ctx.project_id or previous.get('canvas_id') != ctx.canvas_id):
            raise ValueError('相同配音结果 ID 不能绑定不同台词、角色或项目')
        if previous.get('status') == 'completed':
            result = previous['result']
            for item in result['utterances']:
                path, asset = asset_path(store, item['asset_id'])
                if hashlib.sha256(path.read_bytes()).hexdigest() != asset.get('agent_speech', {}).get('sha256'):
                    raise RuntimeError('已注册配音文件损坏，不能作为有效缓存复用')
            return result
    machine = _ready(ctx, previous)
    if not previous:
        store.put('agent_speech_tasks', request['id'], {'id': request['id'], 'fingerprint': request['fingerprint'],
            'account': ctx.account, 'project_id': ctx.project_id, 'canvas_id': ctx.canvas_id,
            'session': machine['session'], 'status': 'pending', 'created_at': time.time(), 'request': request})
    try:
        async with asyncio.timeout(WAIT_SECONDS):
            state = await _execute(ctx, request, bootstrap=True)
            while state['status'] == 'running':
                store.patch('agent_speech_tasks', request['id'], status='running',
                            phase=state.get('phase'), completed_count=state.get('completed_count', 0))
                await asyncio.sleep(POLL_SECONDS)
                state = await _execute(ctx, request)
            if state['status'] != 'completed':
                if state['status'] == 'failed':
                    store.patch('agent_speech_tasks', request['id'], status='failed', phase=state.get('phase'))
                raise RuntimeError('Qwen 配音未完成：' + str(state.get('error') or '原任务需要恢复')[:300])
            outputs = state.get('utterances', [])
            if not outputs:
                outputs = []
                for offset in range(0, len(request['utterances']), 8):
                    page = await _execute(ctx, request, offset=offset)
                    if page['status'] != 'completed' or not isinstance(page.get('utterances'), list):
                        raise RuntimeError('配音完成清单不完整')
                    outputs.extend(page['utterances'])
            store.patch('agent_speech_tasks', request['id'], status='downloading')
            result = await _register(ctx, request, outputs)
            store.patch('agent_speech_tasks', request['id'], status='completed', phase='completed',
                        completed_count=len(result['utterances']), result=result)
            return result
    except BaseException:
        current = store.get('agent_speech_tasks', request['id'])
        if current.get('status') not in {'completed', 'failed'}:
            # WARN: Cancellation only ends local observation; the detached remote task must remain resumable.
            store.patch('agent_speech_tasks', request['id'], status='interrupted',
                        message='本地等待已中断；原配音任务与逐句缓存保留，可继续相同请求')
        raise


async def synthesize(ctx, utterances, result_id):
    request = speech_remote.make_request(result_id, utterances)
    store = ctx.manager.store
    if not hasattr(store, '_speech_locks'):
        store._speech_locks = {}
    lock = store._speech_locks.setdefault(result_id, asyncio.Lock())
    async with lock:
        return await _synthesize(ctx, request)
