import sys
import threading
import time
from types import SimpleNamespace

import pytest

from backend import deploy_remote, video_sr_remote
from backend.telemetry_remote import ModelPreparation


def setup_downloads(tmp_path, monkeypatch, download):
    monkeypatch.setattr(deploy_remote, 'ROOT', tmp_path)
    monkeypatch.setattr(deploy_remote, 'COMFY', tmp_path / 'ComfyUI')
    monkeypatch.setattr(sys, 'argv', ['deploy_remote.py', '--models', 'moss-image'])
    monkeypatch.setitem(sys.modules, 'huggingface_hub', SimpleNamespace(hf_hub_download=download))


def test_uncached_weights_download_concurrently_with_bounded_workers(tmp_path, monkeypatch):
    source = tmp_path / 'weight'
    source.write_bytes(b'weights')
    mutex = threading.Lock()
    active = peak = 0

    def download(*args, **kwargs):
        nonlocal active, peak
        if kwargs.get('local_files_only'):
            raise FileNotFoundError('not cached')
        with mutex:
            active += 1
            peak = max(peak, active)
        time.sleep(.08)
        with mutex:
            active -= 1
        return str(source)

    setup_downloads(tmp_path, monkeypatch, download)
    monkeypatch.setenv('WB_DOWNLOAD_WORKERS', '2')
    records = deploy_remote.download_models()
    assert peak == 2
    assert len(records) == 3
    assert len({record['file'] for record in records}) == 3


def test_pinned_cache_reuse_never_needs_a_network_metadata_request(tmp_path, monkeypatch):
    source = tmp_path / 'weight'
    source.write_bytes(b'cached pinned bytes')

    def download(*args, **kwargs):
        assert kwargs.get('local_files_only'), 'warm deployment must work without network'
        return str(source)

    setup_downloads(tmp_path, monkeypatch, download)
    assert len(deploy_remote.download_models()) == 3
    assert len(deploy_remote.download_models()) == 3


def test_empty_or_failed_weight_never_publishes_ready_assets(tmp_path, monkeypatch):
    source = tmp_path / 'empty'
    source.touch()
    setup_downloads(tmp_path, monkeypatch, lambda *a, **k: str(source))
    with pytest.raises((RuntimeError, ValueError), match='empty|空|有效'):
        deploy_remote.download_models()
    assert not (tmp_path / 'assets.json').exists()


def test_installer_overlaps_weights_and_skips_completed_pinned_environment(tmp_path, monkeypatch):
    setup_downloads(tmp_path, monkeypatch, lambda *a, **k: '')
    comfy = tmp_path / 'ComfyUI'
    comfy.mkdir()
    (comfy / 'requirements.txt').write_text('test-package==1\n')
    python = tmp_path / 'venv/bin/python'
    python.parent.mkdir(parents=True)
    python.touch()
    downloading = threading.Event()
    installed = threading.Event()
    installs = []
    def download():
        downloading.set()
        assert installed.wait(2), 'downloads should overlap package installation'
        return []
    def install(*args):
        installs.append(args)
        assert downloading.wait(2)
        installed.set()
    monkeypatch.setattr(deploy_remote, 'download_models', download)
    monkeypatch.setattr(deploy_remote, 'install_packages', install, raising=False)
    deploy_remote.prepare_environment(python)
    deploy_remote.prepare_environment(python)
    assert len(installs) == 1


def test_video_environment_starts_while_all_download_slots_are_busy(tmp_path, monkeypatch):
    source = tmp_path / 'weight'
    source.write_bytes(b'weights')
    runtime_started = threading.Event()
    def download(*args, **kwargs):
        assert runtime_started.wait(2), 'video environment must not wait behind weight downloads'
        return str(source)
    setup_downloads(tmp_path, monkeypatch, download)
    monkeypatch.setenv('WB_DOWNLOAD_WORKERS', '1')
    monkeypatch.setattr(sys, 'argv', ['deploy_remote.py', '--models', 'seedvr2-3b'])
    monkeypatch.setattr(video_sr_remote, 'prepare_runtime', lambda *args: runtime_started.set())
    assert len(deploy_remote.download_models()) == 2


def test_prepared_models_skip_new_subprocess_but_missing_weight_invalidates_cache(tmp_path, monkeypatch):
    source = tmp_path / 'weight'
    source.write_bytes(b'weights')
    setup_downloads(tmp_path, monkeypatch, lambda *args, **kwargs: str(source))
    deploy_remote.download_models()
    calls = []
    monkeypatch.setattr('backend.telemetry_remote.subprocess.Popen', lambda *args, **kwargs: calls.append(args))
    preparation = ModelPreparation(tmp_path)
    assert preparation.start('b' * 32, ['moss-image'])['phase'] == 'ready'
    assert calls == []
    source.unlink()
    assert preparation.start('c' * 32, ['moss-image'])['phase'] == 'starting'
    assert len(calls) == 1


def test_package_install_reuses_colab_system_environment_without_upgrading_torch(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(deploy_remote, 'ROOT', tmp_path)
    monkeypatch.setattr(deploy_remote, 'run', calls.append)
    deploy_remote.install_packages('/trusted/venv/bin/python', tmp_path / 'requirements.txt')
    command = calls[0]
    assert command[:4] == ['/trusted/venv/bin/python', '-m', 'pip', 'install']
    assert '--no-cache-dir' not in command and '--upgrade' not in command
    assert '--cache-dir' in command and '--prefer-binary' in command


def test_api_only_requirements_skip_cloud_ui_bundles_but_keep_inference_dependencies(tmp_path, monkeypatch):
    comfy = tmp_path / 'ComfyUI'
    comfy.mkdir()
    (comfy / 'requirements.txt').write_text('comfyui-frontend-package==1\ncomfyui-workflow-templates==2\ncomfyui-embedded-docs==3\ntorch\nspandrel\ncomfy-kitchen==0.2.41\n')
    monkeypatch.setattr(deploy_remote, 'ROOT', tmp_path)
    monkeypatch.setattr(deploy_remote, 'COMFY', comfy)
    text = deploy_remote.api_requirements().read_text()
    assert 'comfyui-' not in text
    assert 'torch\n' in text and 'spandrel\n' in text and 'comfy-kitchen==0.2.41' in text


def test_cdn_rate_limit_retries_only_interrupted_weights_and_preserves_cache(tmp_path, monkeypatch):
    source = tmp_path / 'weight'
    source.write_bytes(b'weights')
    calls, delays = {}, []
    def download(repo, filename, **kwargs):
        if kwargs.get('local_files_only'):
            raise FileNotFoundError('not cached')
        calls[filename] = calls.get(filename, 0) + 1
        if 'diffusion_models/' in filename and calls[filename] == 1:
            raise RuntimeError('CAS Client Error: HTTP status client error (429 Too Many Requests)')
        return str(source)
    setup_downloads(tmp_path, monkeypatch, download)
    monkeypatch.setattr(deploy_remote.time, 'sleep', delays.append)
    assert len(deploy_remote.download_models()) == 3
    assert sorted(calls.values()) == [1, 1, 2]
    assert delays and delays[0] >= 5


def test_download_permission_error_fails_without_retries_or_ready_manifest(tmp_path, monkeypatch):
    calls = []
    class Forbidden(Exception):
        response = SimpleNamespace(status_code=403)
    def download(repo, filename, **kwargs):
        if kwargs.get('local_files_only'):
            raise FileNotFoundError('not cached')
        calls.append(filename)
        raise Forbidden('Access denied')
    setup_downloads(tmp_path, monkeypatch, download)
    with pytest.raises(RuntimeError, match='模型下载失败'):
        deploy_remote.download_models()
    assert len(calls) == len(set(calls))
    assert not (tmp_path / 'assets.json').exists()


def test_finishing_video_runtime_never_increases_weight_download_limit(tmp_path, monkeypatch):
    source = tmp_path / 'weight'
    source.write_bytes(b'weights')
    lock = threading.Lock()
    active = peak = 0
    def download(*args, **kwargs):
        nonlocal active, peak
        if kwargs.get('local_files_only'):
            raise FileNotFoundError('not cached')
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(.08)
        with lock:
            active -= 1
        return str(source)
    setup_downloads(tmp_path, monkeypatch, download)
    monkeypatch.setenv('WB_DOWNLOAD_WORKERS', '1')
    monkeypatch.setattr(sys, 'argv', ['deploy_remote.py', '--models', 'seedvr2-3b'])
    monkeypatch.setattr(video_sr_remote, 'prepare_runtime', lambda *args: None)
    deploy_remote.download_models()
    assert peak == 1
