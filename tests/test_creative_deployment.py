import sys
from types import SimpleNamespace

from backend import deploy_remote


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
