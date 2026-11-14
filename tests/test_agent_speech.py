import asyncio
import base64
import contextlib
import io
import json
import math
import re
import subprocess
import sys
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend import agent_speech, speech_remote
from backend.store import Store


def lines():
    return [{'id': 'a-1', 'speaker': 'Serena', 'text': '你为什么还在等？', 'instruct': '轻声、自然。'},
            {'id': 'b-1', 'speaker': 'Uncle_Fu', 'text': '因为有人还没回来。', 'instruct': ''}]


def request_from_script(source):
    encoded = re.search(r"^REQUEST_B64 = '([A-Za-z0-9+/=]+)'$", source, re.M)[1]
    return json.loads(base64.b64decode(encoded))


class Engine:
    def __init__(self):
        self.voices = []

    def generate_custom_voice(self, **kwargs):
        self.voices.append(kwargs['speaker'])
        return [[.1 * math.sin(i / 8) for i in range(2400)]], 24000


@pytest.fixture
def ctx(tmp_path):
    store = Store(tmp_path / 'data')
    store.put('meta', 'install', {'id': 'owned'})
    store.put('machines', 'colab1', {'phase': 'ready', 'session': 'wb-owned-1-test'})
    manager = SimpleNamespace(store=store, active_jobs=lambda account: [],
                              machine=lambda account: store.get('machines', account))
    value = SimpleNamespace(manager=manager, project_id='project', canvas_id='canvas', account='colab1', mode='auto')
    yield value
    store.close()


def test_fixed_voices_and_real_samples_survive_remote_cache(tmp_path):
    request = speech_remote.make_request('a' * 32, lines())
    engine = Engine()
    result = speech_remote.render(request, tmp_path, lambda: engine)
    assert result['status'] == 'completed'
    assert [u['speaker'] for u in result['utterances']] == ['Serena', 'Uncle_Fu']
    assert all(u['samples'] == 2400 and u['sample_rate'] == 24000 and u['duration'] == .1 for u in result['utterances'])
    rerun = speech_remote.render(speech_remote.make_request('b' * 32, lines()), tmp_path,
                                 lambda: pytest.fail('cache reuse must not load the model'))
    assert all(u['cache_hit'] for u in rerun['utterances'])
    assert engine.voices == ['Serena', 'Uncle_Fu']


def test_changed_voice_or_direction_never_reuses_previous_wave(tmp_path):
    engine = Engine()
    speech_remote.render(speech_remote.make_request('a' * 32, lines()), tmp_path, lambda: engine)
    changed = lines()
    changed[0]['speaker'] = 'Vivian'
    changed[1]['instruct'] = '迟疑地回答。'
    result = speech_remote.render(speech_remote.make_request('b' * 32, changed), tmp_path, lambda: engine)
    assert not any(u['cache_hit'] for u in result['utterances'])
    assert engine.voices == ['Serena', 'Uncle_Fu', 'Vivian', 'Uncle_Fu']


def test_remote_failure_retains_completed_utterance_for_resume(tmp_path):
    class Broken(Engine):
        def generate_custom_voice(self, **kwargs):
            if kwargs['speaker'] == 'Uncle_Fu':
                raise RuntimeError('injected inference failure')
            return super().generate_custom_voice(**kwargs)
    with pytest.raises(RuntimeError):
        speech_remote.render(speech_remote.make_request('a' * 32, lines()), tmp_path, Broken)
    engine = Engine()
    resumed = speech_remote.render(speech_remote.make_request('a' * 32, lines()), tmp_path, lambda: engine)
    assert resumed['utterances'][0]['cache_hit']
    assert engine.voices == ['Uncle_Fu']


def test_corrupt_remote_cache_regenerates_only_damaged_line(tmp_path):
    request = speech_remote.make_request('a' * 32, lines())
    result = speech_remote.render(request, tmp_path, Engine)
    first = tmp_path / 'cache' / (result['utterances'][0]['cache_key'] + '.wav')
    first.write_bytes(b'not a wave')
    engine = Engine()
    result = speech_remote.render(request, tmp_path, lambda: engine)
    assert [item['cache_hit'] for item in result['utterances']] == [False, True]
    assert engine.voices == ['Serena']


@pytest.mark.parametrize('value', [[], [0.0] * 240, [float('nan')], [float('inf')]])
def test_invalid_audio_is_never_cached(tmp_path, value):
    class Invalid:
        def generate_custom_voice(self, **kwargs):
            return [value], 24000
    with pytest.raises(ValueError):
        speech_remote.render(speech_remote.make_request('a' * 32, lines()), tmp_path, Invalid)
    assert list((tmp_path / 'cache').glob('*.wav')) == []


def test_request_is_literal_data_in_valid_python_scripts():
    utterances = lines()
    utterances[0]['text'] = '引号\"\'、反引号`和$(echo secret)都必须原样读。'
    utterances[0]['instruct'] = '不要执行 __import__("os").system("bad")'
    request = speech_remote.make_request('a' * 32, utterances)
    for bootstrap, offset in ((True, None), (False, None), (False, 0), (False, 72)):
        source = agent_speech._script(request, bootstrap, offset)
        compile(source, '<speech-bootstrap>', 'exec')
        assert request_from_script(source) == request
        assert utterances[0]['text'] not in source


def test_whitespace_transcript_is_preserved_and_excess_whitespace_is_bounded():
    utterances = lines()
    utterances[0]['text'] = '  原话。\n'
    utterances[0]['instruct'] = ' 轻声。 '
    request = speech_remote.make_request('a' * 32, utterances)
    assert request['utterances'][0] == utterances[0]
    utterances[0]['text'] = ' ' * 10000 + '话'
    with pytest.raises(ValueError):
        speech_remote.make_request('a' * 32, utterances)


@pytest.mark.parametrize('change', ['voice', 'duplicate', 'id', 'empty', 'text_size', 'extra'])
def test_invalid_requests_fail_before_cloud_access(change):
    utterances = lines()
    if change == 'voice':
        utterances[0]['speaker'] = 'fake-voice'
    elif change == 'duplicate':
        utterances[1]['id'] = utterances[0]['id']
    elif change == 'id':
        utterances[0]['id'] = '../../escape'
    elif change == 'empty':
        utterances[0]['text'] = ''
    elif change == 'text_size':
        utterances[0]['text'] = '字' * 301
    else:
        utterances[0]['url'] = 'https://invalid.example/audio'
    with pytest.raises(ValueError):
        speech_remote.make_request('a' * 32, utterances)


class Colab:
    def __init__(self, remote, *, fail_download=False):
        self.remote, self.fail_download = remote, fail_download
        self.starts, self.downloads, self.engine = 0, 0, Engine()
        self.requests = []

    async def execute(self, account, session, script):
        source = Path(script).read_text(encoding='utf-8')
        request = request_from_script(source)
        self.requests.append(request)
        task = self.remote / request['id']
        status = task / 'status.json'
        if not status.exists():
            self.starts += 1
            result = speech_remote.render(request, self.remote, lambda: self.engine)
            task.mkdir(exist_ok=True)
            status.write_text(json.dumps(result), encoding='utf-8')
        return 'safe cli notice\nWB_SPEECH:' + status.read_text(encoding='utf-8')

    async def download(self, account, session, remote, destination):
        self.downloads += 1
        if self.fail_download:
            self.fail_download = False
            raise ConnectionError('temporary transfer failure')
        source = self.remote / 'cache' / Path(remote).name
        Path(destination).write_bytes(source.read_bytes())


async def test_local_synthesis_registers_immutable_transcript_and_does_not_resubmit(ctx, tmp_path):
    ctx.manager.colab = Colab(tmp_path / 'remote')
    result = await agent_speech.synthesize(ctx, lines(), 'a' * 32)
    assert result['lip_sync'] is False
    task = ctx.manager.store.get('agent_speech_tasks', 'a' * 32)
    assert task['completed_count'] == len(lines()) and task['phase'] == 'completed'
    assert [u['speaker'] for u in result['utterances']] == ['Serena', 'Uncle_Fu']
    assert all(u['duration'] == .1 for u in result['utterances'])
    for source, item in zip(lines(), result['utterances']):
        asset = ctx.manager.store.get('assets', item['asset_id'])
        assert asset['agent_speech']['text'] == source['text']
        assert asset['agent_speech']['speaker'] == source['speaker']
        assert asset['agent_speech']['model_revision'] == speech_remote.REVISION
        with wave.open(str(ctx.manager.store.directory / 'assets' / (asset['id'] + '.wav'))) as wav:
            assert wav.getnframes() == 2400
    ctx.manager.store.patch('machines', ctx.account, phase='stopped', session=None)
    replay = await agent_speech.synthesize(ctx, lines(), 'a' * 32)
    assert replay == result
    assert ctx.manager.colab.starts == 1 and ctx.manager.colab.downloads == 2


async def test_same_id_different_text_rejected_without_cloud_call(ctx, tmp_path):
    ctx.manager.colab = Colab(tmp_path / 'remote')
    await agent_speech.synthesize(ctx, lines(), 'a' * 32)
    changed = lines()
    changed[0]['text'] = '另一条台词。'
    with pytest.raises(ValueError, match='不同'):
        await agent_speech.synthesize(ctx, changed, 'a' * 32)
    assert ctx.manager.colab.starts == 1


async def test_completed_asset_corruption_is_not_a_successful_cache_hit(ctx, tmp_path):
    ctx.manager.colab = Colab(tmp_path / 'remote')
    result = await agent_speech.synthesize(ctx, lines(), 'a' * 32)
    key = result['utterances'][0]['asset_id']
    (ctx.manager.store.directory / 'assets' / (key + '.wav')).write_bytes(b'damaged')
    with pytest.raises(RuntimeError, match='损坏'):
        await agent_speech.synthesize(ctx, lines(), 'a' * 32)
    assert ctx.manager.colab.starts == 1


async def test_remote_transcript_substitution_is_rejected(ctx, tmp_path):
    class Substitution(Colab):
        async def execute(self, account, session, script):
            output = await super().execute(account, session, script)
            value = json.loads(output.split('WB_SPEECH:')[1])
            value['utterances'][0]['speaker'] = 'Vivian'
            return 'WB_SPEECH:' + json.dumps(value)
    ctx.manager.colab = Substitution(tmp_path / 'remote')
    with pytest.raises(RuntimeError, match='声线'):
        await agent_speech.synthesize(ctx, lines(), 'a' * 32)
    assert ctx.manager.store.all('assets') == []


async def test_concurrent_same_id_has_one_remote_synthesis(ctx, tmp_path):
    ctx.manager.colab = Colab(tmp_path / 'remote')
    a, b = await asyncio.gather(agent_speech.synthesize(ctx, lines(), 'a' * 32),
                                agent_speech.synthesize(ctx, lines(), 'a' * 32))
    assert a == b
    assert ctx.manager.colab.starts == 1
    assert len(ctx.manager.store.all('assets')) == 2


async def test_completed_manifest_is_paged_through_actual_query_script(ctx, tmp_path):
    class Paged(Colab):
        async def execute(self, account, session, script):
            source = Path(script).read_text(encoding='utf-8')
            request = request_from_script(source)
            task = self.remote / request['id']
            status = task / 'status.json'
            if not status.exists():
                self.starts += 1
                result = speech_remote.render(request, self.remote, lambda: self.engine)
                task.mkdir(exist_ok=True)
                status.write_text(json.dumps(result), encoding='utf-8')
            query = source.split('\nimport fcntl\n')[0]
            if '\nimport fcntl\n' in source:
                query += '\nemit()\n'
            query = query.replace(f'root=Path({speech_remote.REMOTE!r})', f'root=Path({str(self.remote)!r})')
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                exec(compile(query, '<actual-status-query>', 'exec'), {})
            return output.getvalue()
    ctx.manager.colab = Paged(tmp_path / 'remote')
    utterances = [{**lines()[i % 2], 'id': f'line-{i}'} for i in range(10)]
    result = await agent_speech.synthesize(ctx, utterances, 'a' * 32)
    assert [u['id'] for u in result['utterances']] == [f'line-{i}' for i in range(10)]
    assert len(ctx.manager.store.all('assets')) == 10
    assert ctx.manager.colab.starts == 1
    assert len(ctx.manager.colab.engine.voices) == 2


async def test_changed_session_cannot_recover_speech_on_a_new_gpu(ctx, tmp_path):
    ctx.manager.colab = Colab(tmp_path / 'remote', fail_download=True)
    with pytest.raises(ConnectionError):
        await agent_speech.synthesize(ctx, lines(), 'a' * 32)
    ctx.manager.store.patch('machines', ctx.account, session='wb-owned-1-replacement')
    with pytest.raises(RuntimeError, match='原会话'):
        await agent_speech.synthesize(ctx, lines(), 'a' * 32)
    assert ctx.manager.colab.starts == 1


async def test_remote_error_does_not_fall_back_to_a_different_engine(ctx, tmp_path):
    class Failed(Colab):
        async def execute(self, account, session, script):
            request = request_from_script(Path(script).read_text(encoding='utf-8'))
            return 'WB_SPEECH:' + json.dumps({'id': request['id'], 'fingerprint': request['fingerprint'],
                'status': 'failed', 'phase': 'failed', 'error': '固定版本模型不可用', 'utterances': []})
    ctx.manager.colab = Failed(tmp_path / 'remote')
    with pytest.raises(RuntimeError, match='模型不可用'):
        await agent_speech.synthesize(ctx, lines(), 'a' * 32)
    assert ctx.manager.store.all('assets') == []
    assert ctx.manager.store.get('agent_speech_tasks', 'a' * 32)['status'] == 'failed'


async def test_transfer_failure_reentry_reuses_original_remote_task(ctx, tmp_path):
    ctx.manager.colab = Colab(tmp_path / 'remote', fail_download=True)
    with pytest.raises(ConnectionError):
        await agent_speech.synthesize(ctx, lines(), 'a' * 32)
    result = await agent_speech.synthesize(ctx, lines(), 'a' * 32)
    assert result['status'] == 'completed'
    assert ctx.manager.colab.starts == 1
    assert ctx.manager.colab.engine.voices == ['Serena', 'Uncle_Fu']


@pytest.mark.parametrize('state', ['plan', 'busy', 'stopped', 'foreign'])
async def test_unavailable_or_foreign_gpu_never_launches(ctx, state):
    if state == 'plan':
        ctx.mode = 'plan'
    elif state == 'busy':
        ctx.manager.active_jobs = lambda account: [{'id': 'busy'}]
    elif state == 'stopped':
        ctx.manager.store.patch('machines', ctx.account, phase='stopped')
    else:
        ctx.manager.store.patch('machines', ctx.account, session='someone-else')
    with pytest.raises((ValueError, RuntimeError)):
        await agent_speech.synthesize(ctx, lines(), 'a' * 32)
    assert ctx.manager.store.all('assets') == []


async def test_cancel_preserves_task_for_explicit_reentry(ctx, tmp_path, monkeypatch):
    class Waiting(Colab):
        async def execute(self, account, session, script):
            request = request_from_script(Path(script).read_text(encoding='utf-8'))
            self.requests.append(request)
            if not hasattr(self, 'event'):
                self.event = asyncio.Event()
                self.event.set()
            return 'WB_SPEECH:' + json.dumps({'id': request['id'], 'fingerprint': request['fingerprint'],
                                            'status': 'running', 'phase': 'synthesizing', 'utterances': []})
    ctx.manager.colab = Waiting(tmp_path / 'remote')
    monkeypatch.setattr(agent_speech, 'POLL_SECONDS', .01)
    task = asyncio.create_task(agent_speech.synthesize(ctx, lines(), 'a' * 32))
    while not ctx.manager.colab.requests:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    saved = ctx.manager.store.get('agent_speech_tasks', 'a' * 32)
    assert saved['status'] == 'interrupted'
    assert saved['session'] == 'wb-owned-1-test'


def pending_task(ctx, result_id='a' * 32, status='interrupted', session='wb-owned-1-test'):
    request = speech_remote.make_request(result_id, lines())
    return ctx.manager.store.put('agent_speech_tasks', result_id, {'id': result_id,
        'fingerprint': request['fingerprint'], 'account': ctx.account, 'project_id': ctx.project_id,
        'canvas_id': ctx.canvas_id, 'session': session, 'status': status, 'request': request})


class ReadOnlyStatus:
    def __init__(self, status, phase=None):
        self.status, self.phase, self.queries = status, phase or status, []

    async def execute(self, account, session, script):
        source = Path(script).read_text(encoding='utf-8')
        assert 'subprocess.Popen(' not in source
        request = request_from_script(source)
        self.queries.append((account, session, request['id']))
        return 'WB_SPEECH:' + json.dumps({'id': request['id'], 'fingerprint': request['fingerprint'],
            'status': self.status, 'phase': self.phase, 'completed_count': 2, 'utterances': []})


async def test_refresh_generated_is_not_completed_without_local_assets(ctx):
    pending_task(ctx)
    ctx.manager.colab = ReadOnlyStatus('completed')
    result = await agent_speech.refresh_pending(ctx.manager, ctx.account)
    saved = ctx.manager.store.get('agent_speech_tasks', 'a' * 32)
    assert saved['status'] == 'generated' and 'result' not in saved
    assert result == [{'id': 'a' * 32, 'status': 'generated', 'phase': 'completed', 'completed_count': 2}]
    assert ctx.manager.store.all('assets') == []
    assert len(ctx.manager.colab.queries) == 1


async def test_synthesize_can_finish_original_generated_task_after_refresh(ctx, tmp_path):
    pending_task(ctx)
    ctx.manager.colab = ReadOnlyStatus('completed')
    await agent_speech.refresh_pending(ctx.manager, ctx.account)
    ctx.manager.colab = Colab(tmp_path / 'remote')
    result = await agent_speech.synthesize(ctx, lines(), 'a' * 32)
    assert result['status'] == 'completed'
    assert len(result['assets']) == 2


@pytest.mark.parametrize('status,phase,expected', [('running', 'loading', 'running'),
    ('failed', 'failed', 'failed'), ('interrupted', 'missing', 'interrupted')])
async def test_refresh_preserves_uncertainty_and_observes_actual_running_or_failed(ctx, status, phase, expected):
    pending_task(ctx)
    ctx.manager.colab = ReadOnlyStatus(status, phase)
    result = await agent_speech.refresh_pending(ctx.manager, ctx.account)
    assert result[0]['status'] == expected and result[0]['phase'] == phase
    assert ctx.manager.store.get('agent_speech_tasks', 'a' * 32)['status'] == expected


@pytest.mark.parametrize('state', ['new_session', 'foreign_session', 'unavailable', 'corrupt_request'])
async def test_refresh_never_queries_a_replacement_or_foreign_runtime(ctx, state):
    task = pending_task(ctx)
    ctx.manager.colab = ReadOnlyStatus('completed')
    if state == 'new_session':
        ctx.manager.store.patch('machines', ctx.account, session='wb-owned-1-new')
    elif state == 'foreign_session':
        ctx.manager.store.patch('machines', ctx.account, session='other-runtime')
    elif state == 'unavailable':
        ctx.manager.store.patch('machines', ctx.account, session=None)
    else:
        request = task['request']
        request['utterances'][0]['speaker'] = 'Vivian'
        ctx.manager.store.patch('agent_speech_tasks', task['id'], request=request)
    result = await agent_speech.refresh_pending(ctx.manager, ctx.account)
    assert len(result) == 1 and result[0]['status'] == 'interrupted'
    assert ctx.manager.colab.queries == []
    assert ctx.manager.store.get('agent_speech_tasks', task['id'])['status'] == 'interrupted'


async def test_refresh_transport_failure_retains_gpu_occupancy_without_echoing_details(ctx):
    pending_task(ctx, status='running')
    class Unreachable(ReadOnlyStatus):
        async def execute(self, *args):
            raise ConnectionError('https://private.invalid/?token=do-not-print')
    ctx.manager.colab = Unreachable('completed')
    result = await agent_speech.refresh_pending(ctx.manager, ctx.account)
    assert result[0]['status'] == 'running'
    assert 'token' not in json.dumps(result) and 'private.invalid' not in json.dumps(result)
    assert ctx.manager.store.get('agent_speech_tasks', 'a' * 32)['status'] == 'running'


async def test_refresh_does_not_overwrite_concurrent_local_registration(ctx):
    pending_task(ctx)
    class CompletedDuringQuery(ReadOnlyStatus):
        async def execute(self, *args):
            response = await super().execute(*args)
            ctx.manager.store.patch('agent_speech_tasks', 'a' * 32, status='completed', result={'sentinel': True})
            return response
    ctx.manager.colab = CompletedDuringQuery('completed')
    await agent_speech.refresh_pending(ctx.manager, ctx.account)
    task = ctx.manager.store.get('agent_speech_tasks', 'a' * 32)
    assert task['status'] == 'completed' and task['result'] == {'sentinel': True}


@pytest.mark.parametrize('status', ['pending', 'running', 'downloading', 'interrupted'])
async def test_new_speech_cannot_bypass_another_unconfirmed_remote_task(ctx, tmp_path, status):
    pending_task(ctx, status=status)
    ctx.manager.colab = Colab(tmp_path / 'remote')
    with pytest.raises(RuntimeError, match='配音'):
        await agent_speech.synthesize(ctx, lines(), 'b' * 32)
    assert ctx.manager.colab.starts == 0
    assert ctx.manager.store.get('agent_speech_tasks', 'b' * 32) is None


@pytest.mark.parametrize('status', ['generated', 'failed', 'completed'])
async def test_confirmed_finished_remote_task_does_not_block_another_speech(ctx, tmp_path, status):
    pending_task(ctx, status=status)
    ctx.manager.colab = Colab(tmp_path / 'remote')
    result = await agent_speech.synthesize(ctx, lines(), 'b' * 32)
    assert result['status'] == 'completed'


async def test_refresh_ignores_other_accounts_and_confirmed_terminal_tasks(ctx):
    pending_task(ctx, status='generated')
    pending_task(ctx, 'b' * 32, status='failed')
    pending_task(ctx, 'c' * 32, status='completed')
    other = pending_task(ctx, 'd' * 32)
    ctx.manager.store.patch('agent_speech_tasks', other['id'], account='colab2')
    ctx.manager.colab = ReadOnlyStatus('completed')
    assert await agent_speech.refresh_pending(ctx.manager, ctx.account) == []
    assert ctx.manager.colab.queries == []


def test_environment_creation_works_when_colab_has_no_ensurepip(tmp_path, monkeypatch):
    monkeypatch.setattr(speech_remote, 'runtime_versions', lambda: {'torch': '2.14.1+cu130'})
    commands = []
    def colab_run(command, **kwargs):
        commands.append(command)
        if command[1:3] == ['-m', 'venv']:
            if '--without-pip' not in command:
                raise subprocess.CalledProcessError(1, command, output='ensurepip is not available')
            folder = Path(command[-1]) / 'bin'
            folder.mkdir(parents=True)
            (folder / 'python').touch()
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(speech_remote.subprocess, 'run', colab_run)
    python = speech_remote.ensure_environment(tmp_path, io.BytesIO())
    assert python == tmp_path / 'venv-qwen-tts-0.1.1/bin/python'
    assert (python.parent.parent / '.ready.json').is_file()
    assert '--system-site-packages' in commands[0]
    installs = [command for command in commands if command[1:3] == ['-m', 'pip']]
    assert installs and all(command[0] == str(python) for command in installs)
    assert not any(command[0] == sys.executable for command in installs)


def test_installer_constraints_preserve_original_torch_and_cuda_runtime(tmp_path, monkeypatch):
    runtime = {'torch': '2.14.1+cu130', 'torchaudio': '2.11.0+cu130', 'torchvision': '0.29.1+cu130',
               'nvidia-cudnn-cu13': '9.24.0.43', 'cuda-toolkit': '13.0.3', 'triton': '3.8.0'}
    monkeypatch.setattr(speech_remote, 'runtime_versions', lambda: runtime)
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        if command[1:3] == ['-m', 'venv']:
            folder = Path(command[-1]) / 'bin'
            folder.mkdir(parents=True)
            (folder / 'python').touch()
        if command[1:3] == ['-m', 'pip']:
            if '-c' not in command:
                raise RuntimeError('unconstrained installation could resolve a second CUDA runtime')
            constraints = Path(command[command.index('-c') + 1]).read_text(encoding='utf-8').splitlines()
            assert set(constraints) == {f'{name}=={version}' for name, version in runtime.items()}
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(speech_remote.subprocess, 'run', run)
    python = speech_remote.ensure_environment(tmp_path, io.BytesIO())
    marker = json.loads((python.parent.parent / '.ready.json').read_text(encoding='utf-8'))
    assert marker['runtime'] == runtime
    before = len(commands)
    assert speech_remote.ensure_environment(tmp_path, io.BytesIO()) == python
    assert len(commands) == before


def test_runtime_constraints_include_existing_cuda_distributions_only(monkeypatch):
    values = {'Torch': '2.14.1+cu130', 'torchaudio': '2.11.0+cu130', 'torchvision': '0.29.1+cu130',
              'nvidia_cudnn_cu13': '9.24.0.43', 'cuda.bindings': '13.0.3', 'triton': '3.8.0',
              'transformers': '5.22.1', 'numpy': '2.4.6'}
    distributions = [SimpleNamespace(metadata={'Name': name}, version=version) for name, version in values.items()]
    monkeypatch.setattr(speech_remote.metadata, 'distributions', lambda: distributions)
    result = speech_remote.runtime_versions()
    assert result == {'torch': '2.14.1+cu130', 'torchaudio': '2.11.0+cu130',
        'torchvision': '0.29.1+cu130', 'nvidia-cudnn-cu13': '9.24.0.43', 'cuda-bindings': '13.0.3', 'triton': '3.8.0'}


def test_private_speech_diagnostic_keeps_stack_but_removes_credentials(tmp_path):
    try:
        raise RuntimeError('CUDA failed; https://private.invalid/path?token=abc api_key=secret-value Bearer xyz sk-secret-key')
    except RuntimeError as exc:
        speech_remote.save_diagnostic(tmp_path, exc, {'phase': 'loading'})
    diagnostic = json.loads((tmp_path / 'diagnostic.json').read_text(encoding='utf-8'))
    assert diagnostic['exception_type'] == 'RuntimeError' and diagnostic['phase'] == 'loading'
    assert diagnostic['frames'][-1]['name'] == 'test_private_speech_diagnostic_keeps_stack_but_removes_credentials'
    text = json.dumps(diagnostic)
    assert all(secret not in text for secret in ('private.invalid', 'token=abc', 'secret-value', 'Bearer xyz', 'sk-secret-key'))
    assert 'CUDA failed' in diagnostic['message']


def test_gpu_preflight_preserves_comfy_models_when_memory_is_sufficient(monkeypatch):
    monkeypatch.setattr(speech_remote, 'free_gpu_bytes', lambda: 20 * 1024**3)
    monkeypatch.setattr(speech_remote, 'comfy_api', lambda *args, **kwargs: pytest.fail('must preserve warm models'))
    result = speech_remote.prepare_gpu_memory()
    assert result['unloaded_comfy'] is False and result['free_bytes'] == 20 * 1024**3


def test_gpu_preflight_releases_only_idle_comfy_weights_when_necessary(monkeypatch):
    values = iter([10 * 1024**3, 10 * 1024**3, 90 * 1024**3])
    monkeypatch.setattr(speech_remote, 'free_gpu_bytes', lambda: next(values))
    calls = []
    def api(method, path, body=None):
        calls.append((method, path, body))
        return {'queue_running': [], 'queue_pending': []} if path == '/queue' else {}
    monkeypatch.setattr(speech_remote, 'comfy_api', api)
    monkeypatch.setattr(speech_remote.time, 'sleep', lambda _: None)
    result = speech_remote.prepare_gpu_memory()
    assert calls == [('GET', '/queue', None), ('POST', '/free', {'unload_models': True, 'free_memory': True})]
    assert result['unloaded_comfy'] is True and result['free_bytes'] == 90 * 1024**3
    assert result['free_before_bytes'] == 10 * 1024**3


@pytest.mark.parametrize('queue', [{'queue_running': [['active']], 'queue_pending': []},
    {'queue_running': [], 'queue_pending': [['queued']]}, {'invalid': []}])
def test_gpu_preflight_never_unloads_busy_or_unknown_comfy_queue(monkeypatch, queue):
    monkeypatch.setattr(speech_remote, 'free_gpu_bytes', lambda: 368 * 1024**2)
    calls = []
    def api(method, path, body=None):
        calls.append((method, path))
        return queue
    monkeypatch.setattr(speech_remote, 'comfy_api', api)
    with pytest.raises(RuntimeError):
        speech_remote.prepare_gpu_memory()
    assert calls == [('GET', '/queue')]


def test_nvidia_preflight_does_not_initialize_cuda_context(monkeypatch):
    def command(args, **kwargs):
        assert args[0] == 'nvidia-smi' and kwargs['timeout'] <= 10
        return SimpleNamespace(stdout='368\n', returncode=0)
    monkeypatch.setattr(speech_remote.subprocess, 'run', command)
    assert speech_remote.free_gpu_bytes() == 368 * 1024**2
