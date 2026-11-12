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
    monkeypatch.setattr(video_sr_remote, 'prepare_runtime', lambda status: None)
    monkeypatch.setattr(sys, 'argv', ['deploy_remote.py', '--models', ','.join(video_sr_remote.MODEL_PRESETS)])
    records = deploy_remote.download_models()
    assert len(records) == 4
    assert sum(filename == video_sr_remote.VAE_FILE for _, filename, _ in calls) == 1
    assert all(revision == video_sr_remote.WEIGHTS_REVISION for _, _, revision in calls)
    assert all((tmp_path / 'ComfyUI/models' / r['file']).read_bytes() == b'model fixture' for r in records)


def test_colab_video_sr_venv_does_not_require_system_ensurepip(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(video_sr_remote, 'ROOT', tmp_path)
    monkeypatch.setattr(video_sr_remote.subprocess, 'run', lambda args, **kw: calls.append(args))
    video_sr_remote.prepare_runtime(lambda *args: None)
    venv = next(args for args in calls if 'venv' in args)
    assert '--without-pip' in venv and '--system-site-packages' in venv
    video_sr_remote.prepare_runtime(lambda *args: None)
    assert sum('venv' in args for args in calls) == 1
