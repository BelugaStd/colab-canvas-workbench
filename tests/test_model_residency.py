import json
from types import SimpleNamespace

import pytest

from backend import deploy_remote, minimax_full_remote, video_sr_remote


def test_comfy_keeps_inactive_model_cache_until_actual_ram_pressure(tmp_path, monkeypatch):
    comfy = tmp_path / 'ComfyUI'
    comfy.mkdir()
    (tmp_path / 'assets.json').write_text(json.dumps([]))
    monkeypatch.setattr(deploy_remote, 'ROOT', tmp_path)
    monkeypatch.setattr(deploy_remote, 'COMFY', comfy)
    commands = []
    def launch(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(pid=123, poll=lambda: None)
    monkeypatch.setattr(deploy_remote.subprocess, 'Popen', launch)
    monkeypatch.setattr(deploy_remote, 'healthy', lambda: True)
    deploy_remote.download_and_serve(prepare=False)
    command = commands[0]
    index = command.index('--cache-ram')
    active, inactive = map(float, command[index + 1:index + 3])
    assert 2 <= inactive <= active <= 10
    assert not {'--cache-none', '--disable-smart-memory', '--highvram'} & set(command)


def test_old_runtime_with_loader_but_without_memory_policy_needs_upgrade(monkeypatch):
    monkeypatch.setattr(minimax_full_remote, 'remote_json', lambda path:
                        {minimax_full_remote.NODE: {}} if path.startswith('/object_info') else
                        {'queue_running': [[1, 'active-user-job']], 'queue_pending': []})
    with pytest.raises(RuntimeError, match='任务'):
        minimax_full_remote.ensure_runtime()


class Memory:
    def __init__(self, free):
        self.free = free
        self.resident = 60 * 2**30

    def get_torch_device(self):
        return 'cuda:0'

    def free_memory(self, required, device):
        assert device == 'cuda:0'
        removed = min(self.resident, max(0, required - self.free))
        self.resident -= removed
        self.free += removed

    def soft_empty_cache(self):
        pass

    def throw_exception_if_processing_interrupted(self):
        pass


@pytest.mark.parametrize('free', [40, 8])
def test_video_sr_only_offloads_shortfall(tmp_path, monkeypatch, free):
    memory = Memory(free * 2**30)
    calls = []
    monkeypatch.setattr(video_sr_remote, 'run_inference', lambda *args: calls.append(args))
    video_sr_remote.run_with_memory_budget([], tmp_path / 'log', memory, 32 * 2**30)
    assert memory.resident == (60 - max(0, 32 - free)) * 2**30
    assert len(calls) == 1


def test_video_sr_retries_only_confirmed_gpu_oom_once(tmp_path, monkeypatch, caplog):
    memory = Memory(40 * 2**30)
    calls = []
    def infer(*args):
        calls.append(memory.resident)
        if len(calls) == 1:
            raise video_sr_remote.VideoSROutOfMemory('CUDA out of memory')
    monkeypatch.setattr(video_sr_remote, 'run_inference', infer)
    video_sr_remote.run_with_memory_budget([], tmp_path / 'log', memory, 32 * 2**30)
    assert calls == [60 * 2**30, 0]
    assert '显存不足' in caplog.text


@pytest.mark.parametrize('error', [RuntimeError('bad video'), KeyboardInterrupt()])
def test_video_sr_does_not_retry_failures_or_cancellation(tmp_path, monkeypatch, error):
    memory = Memory(40 * 2**30)
    calls = []
    def infer(*args):
        calls.append(True)
        raise error
    monkeypatch.setattr(video_sr_remote, 'run_inference', infer)
    with pytest.raises(type(error)):
        video_sr_remote.run_with_memory_budget([], tmp_path / 'log', memory, 32 * 2**30)
    assert len(calls) == 1
    assert memory.resident == 60 * 2**30


def test_video_sr_stops_after_second_oom_and_clears_partial_output_before_retry(tmp_path, monkeypatch):
    memory = Memory(40 * 2**30)
    output = tmp_path / 'enhanced.mp4'
    log = tmp_path / 'inference.log'
    calls = []
    def infer(*args):
        assert not output.exists()
        output.write_bytes(b'incomplete video')
        log.write_text('CUDA out of memory')
        calls.append(True)
        raise video_sr_remote.VideoSROutOfMemory('CUDA out of memory')
    monkeypatch.setattr(video_sr_remote, 'run_inference', infer)
    with pytest.raises(video_sr_remote.VideoSROutOfMemory):
        video_sr_remote.run_with_memory_budget([], log, memory, destination=output)
    assert len(calls) == 2
    assert log.with_suffix('.oom.log').read_text() == 'CUDA out of memory'


@pytest.mark.parametrize('message,error', [
    ('torch.OutOfMemoryError: CUDA out of memory.', 'oom'),
    ('RuntimeError: CUDA error: out of memory', 'oom'),
    ('RuntimeError: codec not found', 'other'),
    ('DefaultCPUAllocator: cannot allocate memory', 'other'),
    ('torch.OutOfMemoryError: CPU allocator cannot allocate memory', 'other'),
])
def test_cli_failure_classifies_gpu_oom_without_retrying_other_errors(tmp_path, monkeypatch, message, error):
    def launch(*args, **kwargs):
        kwargs['stdout'].write(message.encode())
        kwargs['stdout'].flush()
        return SimpleNamespace(poll=lambda: 1, returncode=1)
    monkeypatch.setattr(video_sr_remote.subprocess, 'Popen', launch)
    expected = video_sr_remote.VideoSROutOfMemory if error == 'oom' else RuntimeError
    with pytest.raises(expected) as captured:
        video_sr_remote.run_inference([], tmp_path / 'log', lambda: None)
    assert type(captured.value) is expected
