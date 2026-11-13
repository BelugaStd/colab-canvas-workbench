import sys
from types import SimpleNamespace

from backend import deploy_remote, video_sr_remote
from backend.config import CREATIVE_MODELS
from backend.telemetry_remote import ModelPreparation


def test_every_canvas_model_is_accepted_by_remote_preparation(tmp_path,monkeypatch):
    calls=[]
    monkeypatch.setattr('backend.telemetry_remote.subprocess.Popen',lambda args,**kwargs:calls.append(args))
    result=ModelPreparation(tmp_path).start('a'*32,CREATIVE_MODELS)
    assert result['phase']=='starting'
    assert set(calls[0][-1].split(','))==set(CREATIVE_MODELS)


def test_model_preparation_is_repeatable_after_server_start(tmp_path, monkeypatch):
    source = tmp_path / 'weights.safetensors'
    source.write_bytes(b'model fixture')
    monkeypatch.setitem(sys.modules, 'huggingface_hub', SimpleNamespace(hf_hub_download=lambda *a, **kw: str(source)))
    monkeypatch.setattr(deploy_remote, 'ROOT', tmp_path)
    monkeypatch.setattr(deploy_remote, 'COMFY', tmp_path / 'ComfyUI')
    monkeypatch.setattr(sys, 'argv', ['deploy_remote.py', '--models', 'moss-image'])
    first = deploy_remote.download_models()
    second = deploy_remote.download_models()
    assert first == second
    assert len(first) == 3
    for record in first:
        assert (tmp_path / 'ComfyUI/models' / record['file']).read_bytes() == b'model fixture'


def test_video_sr_preparation_uses_pinned_weights_and_shared_vae(tmp_path, monkeypatch):
    source = tmp_path / 'weights.safetensors'
    source.write_bytes(b'model fixture')
    calls = []
    def download(repo, filename, **kwargs):
        calls.append((repo, filename, kwargs['revision']))
        return str(source)
    monkeypatch.setitem(sys.modules, 'huggingface_hub', SimpleNamespace(hf_hub_download=download))
    monkeypatch.setattr(deploy_remote, 'ROOT', tmp_path)
    monkeypatch.setattr(deploy_remote, 'COMFY', tmp_path / 'ComfyUI')
    monkeypatch.setattr(video_sr_remote, 'prepare_runtime', lambda status, installer=None: None)
    monkeypatch.setattr(sys, 'argv', ['deploy_remote.py', '--models', ','.join(video_sr_remote.MODEL_PRESETS)])
    records = deploy_remote.download_models()
    assert len(records) == 4
    assert sum(filename == video_sr_remote.VAE_FILE for _, filename, _ in calls) == 1
    assert all(revision == video_sr_remote.WEIGHTS_REVISION for _, _, revision in calls)
    assert all((tmp_path / 'ComfyUI/models' / r['file']).read_bytes() == b'model fixture' for r in records)


def test_colab_video_sr_venv_does_not_require_system_ensurepip(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(video_sr_remote, 'ROOT', tmp_path)
    def run(args, **kwargs):
        calls.append(args)
        if 'venv' in args:
            python = tmp_path / 'video-sr-venv/bin/python'
            python.parent.mkdir(parents=True, exist_ok=True)
            python.touch()
    monkeypatch.setattr(video_sr_remote.subprocess, 'run', run)
    video_sr_remote.prepare_runtime(lambda *args: None)
    venv = next(args for args in calls if 'venv' in args)
    assert '--without-pip' in venv and '--system-site-packages' in venv
    video_sr_remote.prepare_runtime(lambda *args: None)
    assert sum('venv' in args for args in calls) == 1
    (tmp_path / 'video-sr-venv/bin/python').unlink()
    video_sr_remote.prepare_runtime(lambda *args: None)
    assert sum('venv' in args for args in calls) == 2


async def test_legacy_sidecar_prepares_new_model_without_restarting_comfy_or_gpu(tmp_path, monkeypatch):
    from aiohttp import web

    from backend.manager import Manager
    from backend.store import Store
    from tests.fake_colab import PROFILES, FakeColab

    fake, store = FakeColab(), Store(tmp_path)
    manager = Manager(store, fake, PROFILES)
    await manager.open()
    await manager.start('colab2')
    original = manager.machine('colab2').copy()
    upgraded = []
    async def status(request):
        return web.json_response({'phase': 'ready' if upgraded else 'missing'})
    async def prepare(request):
        return web.json_response({'error': 'Invalid model preset'}, status=400)
    app = web.Application()
    app.router.add_get('/models/{key}', status)
    app.router.add_post('/models/{key}', prepare)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    manager.machine('colab2', telemetry_port=site._server.sockets[0].getsockname()[1])
    async def execute(account, session, script):
        assert session == original['session']
        source = script.read_text()
        compile(source, str(script), 'exec')
        assert '--models-only' in source
        assert 'server.pid' not in source and 'kill' not in source
        upgraded.append(source)
        return 'Model preparation started'
    monkeypatch.setattr(fake, 'execute', execute)
    try:
        await manager.prepare_models(account='colab2', models=['qwen-edit'])
        assert len(upgraded) == 1
        assert manager.machine('colab2')['session'] == original['session']
        assert manager.machine('colab2')['port'] == original['port']
        assert sum(c[1] == 'create' for c in fake.calls) == 1
        assert not fake.prompt_posts
    finally:
        await runner.cleanup()
        await manager.close()
        store.close()
