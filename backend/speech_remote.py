"""Fixed-voice Qwen speech worker, executed only in an already owned runtime.

API: https://github.com/QwenLM/Qwen3-TTS#python-package-usage
"""
import array
import hashlib
import importlib.metadata as metadata
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
import urllib.request
import wave
from pathlib import Path

REPOSITORY = 'Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice'
REVISION = '0c0e3051f131929182e2c023b9537f8b1c68adfe'
SDK_VERSION = '0.1.1'
MODEL_BYTES = 4520218951
WEIGHTS = {'model.safetensors': 3833402552, 'speech_tokenizer/model.safetensors': 682293092}
VOICES = ('Serena', 'Vivian', 'Uncle_Fu', 'Dylan', 'Eric', 'Ryan', 'Aiden', 'Ono_Anna', 'Sohee')
GENERATION = {'max_new_tokens': 2048}
VERSION = 'qwen-fixed-voice-v1'
REMOTE = '/content/colab-workbench/ComfyUI/output/workbench-speech'
MIN_GPU_BYTES = 12 * 1024**3


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


def make_request(result_id, utterances):
    if not isinstance(result_id, str) or not re.fullmatch('[0-9a-f]{32}', result_id):
        raise ValueError('配音结果 ID 必须为 32 位十六进制')
    if not isinstance(utterances, list) or not 1 <= len(utterances) <= 80:
        raise ValueError('逐句配音需要 1–80 条台词')
    seen, normalized = set(), []
    for item in utterances:
        if not isinstance(item, dict) or set(item) - {'id', 'speaker', 'text', 'instruct'}:
            raise ValueError('台词只接受 id、speaker、text、instruct')
        key, speaker, text, instruct = (item.get('id'), item.get('speaker'), item.get('text'), item.get('instruct', ''))
        if not isinstance(key, str) or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_-]{0,63}', key) or key in seen:
            raise ValueError('台词 ID 必须唯一且不能含路径')
        if speaker not in VOICES:
            raise ValueError('需要选择 Qwen 官方固定声线')
        if (not isinstance(text, str) or not text.strip() or not 1 <= len(text) <= 300
                or not isinstance(instruct, str) or len(instruct) > 300
                or any(ord(c) < 32 and c not in '\n\t' for c in text + instruct)):
            raise ValueError('每句台词需 1–300 字，表演指令最多 300 字，不能包含控制字符')
        seen.add(key)
        normalized.append({'id': key, 'speaker': speaker, 'text': text, 'instruct': instruct})
    if sum(len(u['text']) for u in normalized) > 12000:
        raise ValueError('单次配音台词总长不能超过 12000 字')
    body = {'version': VERSION, 'model': REPOSITORY, 'model_revision': REVISION,
            'sdk_version': SDK_VERSION, 'language': 'Chinese', 'generation': GENERATION,
            'utterances': normalized}
    return {'id': result_id, 'fingerprint': digest(body), **body}


def cache_key(utterance):
    return digest({'version': VERSION, 'model_revision': REVISION, 'sdk_version': SDK_VERSION,
                   'language': 'Chinese', 'generation': GENERATION,
                   **{k: utterance[k] for k in ('speaker', 'text', 'instruct')}})


def atomic_json(path, value):
    temporary = path.with_suffix('.json.part')
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    temporary.replace(path)


def save_diagnostic(directory, exc, state):
    message = re.sub(r'https?://[^\s\"\']+', '[redacted-url]', str(exc))
    message = re.sub(r'(?i)bearer\s+[^\s,;\"\'}]+', 'Bearer [redacted]', message)
    message = re.sub(r'(?i)(api[_-]?key|token|secret|authorization)([\"\s:=]+)[^\s,;\"\'}]+',
                     r'\1\2[redacted]', message)
    message = re.sub(r'\bsk-[\w-]+', '[redacted]', message)
    frames = [{'file': frame.filename, 'line': frame.lineno, 'name': frame.name}
              for frame in traceback.extract_tb(exc.__traceback__)]
    atomic_json(directory / 'diagnostic.json', {'exception_type': type(exc).__name__, 'message': message[:4000],
        'phase': state.get('phase'), 'completed_count': len(state.get('utterances', [])), 'frames': frames,
        'created_at': time.time()})


def inspect_wave(path):
    with wave.open(str(path), 'rb') as stream:
        rate, samples = stream.getframerate(), stream.getnframes()
        if (stream.getnchannels() != 1 or stream.getsampwidth() != 2 or stream.getcomptype() != 'NONE'
                or not 8000 <= rate <= 96000 or not 0 < samples <= rate * 120
                or len(stream.readframes(samples)) != samples * 2):
            raise ValueError('配音必须为完整的单声道 PCM16 WAV，单句不超过 120 秒')
    return {'samples': samples, 'sample_rate': rate, 'duration': samples / rate,
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def write_wave(path, waveform, rate):
    if type(rate) is not int or not 8000 <= rate <= 96000 or not 0 < len(waveform) <= rate * 120:
        raise ValueError('模型返回无效音频长度或采样率')
    values, peak = array.array('h'), 0.0
    for sample in waveform:
        value = float(sample)
        if not math.isfinite(value):
            raise ValueError('模型音频包含非有限样本')
        peak = max(peak, abs(value))
        values.append(round(max(-1.0, min(1.0, value)) * 32767))
    if peak < 1e-5:
        raise ValueError('模型返回全静音，未注册为成功配音')
    if sys.byteorder != 'little':
        values.byteswap()
    temporary = path.with_suffix('.wav.part')
    with wave.open(str(temporary), 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(values.tobytes())
    temporary.replace(path)


def render(request, root, engine_factory, progress=None, seed=None):
    validated = make_request(request['id'], request['utterances'])
    if validated != request:
        raise ValueError('配音请求指纹不一致')
    root = Path(root)
    cache = root / 'cache'
    cache.mkdir(parents=True, exist_ok=True)
    result = {'id': request['id'], 'fingerprint': request['fingerprint'], 'status': 'running',
              'phase': 'synthesizing', 'utterances': []}
    engine = None
    for item in request['utterances']:
        key = cache_key(item)
        path, metadata = cache / (key + '.wav'), cache / (key + '.json')
        info, hit = None, False
        if path.is_file() and metadata.is_file():
            try:
                info = inspect_wave(path)
                hit = json.loads(metadata.read_text(encoding='utf-8')) == info
            except (ValueError, OSError, EOFError, wave.Error):
                pass
        if not hit:
            if engine is None:
                engine = engine_factory()
            if seed:
                seed(int(key[:8], 16))
            waves, rate = engine.generate_custom_voice(text=item['text'], speaker=item['speaker'],
                language='Chinese', instruct=item['instruct'], **GENERATION)
            if len(waves) != 1:
                raise ValueError('模型返回的台词数量不匹配')
            write_wave(path, waves[0], rate)
            info = inspect_wave(path)
            atomic_json(metadata, info)
        result['utterances'].append({**item, **info, 'cache_key': key, 'cache_hit': hit})
        if progress:
            progress(result)
    result.update(status='completed', phase='completed')
    if progress:
        progress(result)
    return result


def model_ready(directory):
    try:
        marker = json.loads((directory / '.workbench-revision.json').read_text(encoding='utf-8'))
        return marker == {'revision': REVISION} and all((directory / name).stat().st_size == size
                                                       for name, size in WEIGHTS.items())
    except (OSError, ValueError):
        return False


def runtime_versions():
    versions = {}
    for distribution in metadata.distributions():
        name = re.sub(r'[-_.]+', '-', distribution.metadata.get('Name', '')).lower()
        if name in {'torch', 'torchaudio', 'torchvision', 'triton'} or name.startswith(('nvidia-', 'cuda-')):
            version = distribution.version
            if not re.fullmatch(r'[A-Za-z0-9.!+_-]+', version):
                raise RuntimeError('speech_runtime_version_invalid')
            versions.setdefault(name, version)
    if 'torch' not in versions:
        raise RuntimeError('speech_runtime_missing')
    return dict(sorted(versions.items()))


def ensure_environment(root, log):
    environment = root / 'venv-qwen-tts-0.1.1'
    python = environment / 'bin/python'
    marker = environment / '.ready.json'
    required = {'qwen-tts': SDK_VERSION, 'transformers': '4.57.3', 'accelerate': '1.12.0'}
    runtime = runtime_versions()
    expected = {**required, 'runtime': runtime}
    if marker.is_file() and python.is_file() and json.loads(marker.read_text()) == expected:
        return python
    if not python.exists():
        # WARN: Colab omits ensurepip; inherit its pip while keeping all installed packages inside this venv.
        subprocess.run([sys.executable, '-m', 'venv', '--without-pip', '--system-site-packages', str(environment)],
                       stdout=log, stderr=subprocess.STDOUT, check=True, timeout=120)
    constraints = environment / 'runtime-constraints.txt'
    constraints.write_text(''.join(f'{name}=={version}\n' for name, version in runtime.items()), encoding='utf-8')
    # NOTE: Isolate Qwen's older Transformers while reusing the runtime's installed CUDA/Torch wheels.
    subprocess.run([str(python), '-m', 'pip', '--isolated', 'install', '--index-url', 'https://pypi.org/simple',
                    '--disable-pip-version-check', '--no-input', '--no-compile', '-c', str(constraints),
                    'qwen-tts==' + SDK_VERSION],
                   stdout=log, stderr=subprocess.STDOUT, check=True, timeout=900)
    check = ('import importlib.metadata as m; import torch,torchaudio; from qwen_tts import Qwen3TTSModel; '
             f'assert {{k:m.version(k) for k in {tuple(required)!r}}} == {required!r}; '
             f'assert {{k:m.version(k) for k in {tuple(runtime)!r}}} == {runtime!r}')
    subprocess.run([str(python), '-c', check], stdout=log, stderr=subprocess.STDOUT, check=True, timeout=90)
    atomic_json(marker, expected)
    return python


def free_gpu_bytes():
    output = subprocess.run(['nvidia-smi', '--id=0', '--query-gpu=memory.free', '--format=csv,noheader,nounits'],
                            check=True, capture_output=True, text=True, timeout=10).stdout.strip()
    if not re.fullmatch(r'\d+', output):
        raise RuntimeError('speech_gpu_memory_unknown')
    return int(output) * 1024**2


def comfy_api(method, path, body=None):
    request = urllib.request.Request('http://127.0.0.1:8188' + path, method=method,
        data=json.dumps(body).encode() if body is not None else None, headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=5) as response:
        data = response.read()
    return json.loads(data) if data else {}


def prepare_gpu_memory(required=MIN_GPU_BYTES):
    # WARN: CUDA context creation can itself OOM; measure with NVML before importing the model or initializing Torch CUDA.
    available = free_gpu_bytes()
    before = available
    if available >= required:
        return {'unloaded_comfy': False, 'free_before_bytes': before, 'free_bytes': available}
    queue = comfy_api('GET', '/queue')
    if (not isinstance(queue, dict) or any(not isinstance(queue.get(key), list) for key in ('queue_running', 'queue_pending'))
            or queue['queue_running'] or queue['queue_pending']):
        raise RuntimeError('speech_comfy_busy')
    comfy_api('POST', '/free', {'unload_models': True, 'free_memory': True})
    for _ in range(20):
        available = free_gpu_bytes()
        if available >= required:
            return {'unloaded_comfy': True, 'free_before_bytes': before, 'free_bytes': available}
        time.sleep(1)
    raise RuntimeError('speech_gpu_memory_insufficient')


def load_engine(root):
    import torch
    from huggingface_hub import snapshot_download
    from qwen_tts import Qwen3TTSModel

    if not torch.cuda.is_available():
        raise RuntimeError('speech_cuda_unavailable')
    if torch.cuda.mem_get_info()[0] < MIN_GPU_BYTES:
        raise RuntimeError('speech_gpu_memory_insufficient')
    directory = root / 'models' / ('qwen3-tts-1.7b-' + REVISION)
    missing = 0 if model_ready(directory) else MODEL_BYTES
    if shutil.disk_usage(root).free < missing + 2 * 1024**3:
        raise RuntimeError('speech_disk_insufficient')
    if not model_ready(directory):
        directory.mkdir(parents=True, exist_ok=True)
        snapshot_download(REPOSITORY, revision=REVISION, local_dir=str(directory), max_workers=4)
        if not all((directory / name).stat().st_size == size for name, size in WEIGHTS.items()):
            raise RuntimeError('speech_weights_incomplete')
        atomic_json(directory / '.workbench-revision.json', {'revision': REVISION})
    model = Qwen3TTSModel.from_pretrained(str(directory), device_map='cuda:0', dtype=torch.bfloat16,
                                        attn_implementation='sdpa', local_files_only=True)
    available = {str(v).lower() for v in model.get_supported_speakers()}
    if not {v.lower() for v in VOICES}.issubset(available):
        raise RuntimeError('speech_voices_unavailable')
    if 'chinese' not in {str(v).lower() for v in model.get_supported_languages()}:
        raise RuntimeError('speech_chinese_unavailable')
    return model


def main():
    import fcntl
    import signal

    request_path = Path(sys.argv[2])
    root = request_path.parent.parent
    request = json.loads(request_path.read_text(encoding='utf-8'))
    status = request_path.parent / 'status.json'
    inherited = int(os.environ['WB_SPEECH_LOCK_FD'])
    os.set_inheritable(inherited, True)
    # WARN: The inherited task lock must survive installation and exec, so repeated launches cannot duplicate synthesis.
    if 'WB_SPEECH_GPU_FD' in os.environ:
        global_lock = os.fdopen(int(os.environ['WB_SPEECH_GPU_FD']), 'a')
    else:
        global_lock = (root / '.gpu.lock').open('a')
        try:
            fcntl.flock(global_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            atomic_json(status, {'id': request['id'], 'fingerprint': request['fingerprint'], 'status': 'failed',
                                 'phase': 'busy', 'error': '另一项配音正在使用 GPU，未重复启动', 'utterances': []})
            return
        os.environ['WB_SPEECH_GPU_FD'] = str(global_lock.fileno())
    os.set_inheritable(global_lock.fileno(), True)
    previous = json.loads(status.read_text(encoding='utf-8'))
    previous.update(id=request['id'], fingerprint=request['fingerprint'], status='running')

    def progress(value):
        previous.update(value, updated_at=time.time())
        atomic_json(status, previous)

    try:
        def deadline(signum, frame):
            raise TimeoutError('speech_worker_timeout')
        signal.signal(signal.SIGALRM, deadline)
        signal.alarm(max(1, int(7200 - (time.time() - previous.get('started_at', time.time())))))
        if '--worker' not in sys.argv:
            progress({'phase': 'installing'})
            model_dir = root / 'models' / ('qwen3-tts-1.7b-' + REVISION)
            missing = 0 if model_ready(model_dir) else MODEL_BYTES
            if shutil.disk_usage(root).free < missing + 2 * 1024**3:
                raise RuntimeError('speech_disk_insufficient')
            with (request_path.parent / 'install.log').open('ab') as log:
                python = ensure_environment(root, log)
            os.execv(str(python), [str(python), __file__, '--run', str(request_path), '--worker'])
        progress({'phase': 'checking_memory'})
        memory = prepare_gpu_memory()
        progress({'memory': memory})
        import torch
        progress({'phase': 'loading'})
        render(request, root, lambda: load_engine(root), progress, torch.manual_seed)
    except Exception as exc:
        save_diagnostic(request_path.parent, exc, previous)
        known = {'speech_cuda_unavailable': '当前会话没有可用 CUDA GPU',
                 'speech_gpu_memory_insufficient': 'GPU 空闲显存不足 12 GiB，未更换引擎或删除磁盘权重',
                 'speech_disk_insufficient': '语音缓存磁盘空间不足，未删除现有模型',
                 'speech_weights_incomplete': '固定版本语音权重未下载完整',
                 'speech_voices_unavailable': '官方固定声线列表不匹配',
                 'speech_chinese_unavailable': '当前语音模型未提供中文能力'}
        known.update(speech_runtime_missing='原运行环境缺少 Torch，未下载替代 CUDA 环境',
                     speech_runtime_version_invalid='原运行环境版本信息无效，未修改 CUDA 环境',
                     speech_gpu_memory_unknown='无法确认 GPU 空闲显存，未启动配音',
                     speech_comfy_busy='ComfyUI 队列仍在运行或状态不明，未卸载模型或启动配音')
        message = known.get(str(exc), 'Qwen 配音失败（' + type(exc).__name__ + '），未切换其它引擎')
        progress({'status': 'failed', 'phase': 'failed', 'error': message})


if __name__ == '__main__':
    main()
