"""Pinned SeedVR2 runtime and the single trusted ComfyUI video SR node."""
import json
import math
import os
import signal
import subprocess
import tempfile
import time
import uuid
from fractions import Fraction
from pathlib import Path

ROOT = Path('/content/colab-workbench')
CODE_REPOSITORY = 'https://github.com/numz/ComfyUI-SeedVR2_VideoUpscaler.git'
CODE_REVISION = '4490bd1f482e026674543386bb2a4d176da245b9'
WEIGHTS_REPOSITORY = 'numz/SeedVR2_comfyUI'
WEIGHTS_REVISION = '09ced71023636e9bc8cdf9cdecfb2625d1e691e8'
MODEL_PRESETS = {
    'seedvr2-7b': 'seedvr2_ema_7b_fp16.safetensors',
    'seedvr2-7b-sharp': 'seedvr2_ema_7b_sharp_fp16.safetensors',
    'seedvr2-3b': 'seedvr2_ema_3b_fp16.safetensors',
}
VAE_FILE = 'ema_vae_fp16.safetensors'


def prepare_runtime(status):
    code = ROOT / 'SeedVR2'
    marker = ROOT / 'video-sr-ready'
    if marker.exists() and marker.read_text() == CODE_REVISION:
        return
    status('installing', '配置 SeedVR2 视频超分环境')
    code.mkdir(parents=True, exist_ok=True)
    if not (code / '.git').exists():
        subprocess.run(['git', '-C', str(code), 'init'], check=True)
        subprocess.run(['git', '-C', str(code), 'remote', 'add', 'origin', CODE_REPOSITORY], check=True)
    subprocess.run(['git', '-C', str(code), 'fetch', '--depth', '1', 'origin', CODE_REVISION], check=True)
    subprocess.run(['git', '-C', str(code), 'checkout', '--detach', CODE_REVISION], check=True)
    python = ROOT / 'video-sr-venv/bin/python'
    if not python.exists():
        import sys
        subprocess.run([sys.executable, '-m', 'venv', '--without-pip', '--system-site-packages', str(python.parents[1])], check=True)
    subprocess.run([str(python), '-m', 'pip', 'install', '--no-cache-dir', '-r', str(code / 'requirements.txt')], check=True)
    marker.write_text(CODE_REVISION)


def output_dimensions(width, height, scale):
    if type(scale) is not int or scale not in (2, 4):
        raise ValueError('超分倍数可选 2× / 4×')
    if min(width, height) <= 0:
        raise ValueError('视频尺寸无效')
    width, height = width * scale, height * scale
    limit = min(1, 3840 / max(width, height), math.sqrt(3840 * 2160 / (width * height)))
    return max(2, int(width * limit / 2) * 2), max(2, int(height * limit / 2) * 2)


def probe_video(path):
    result = subprocess.run(['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(path)],
                            capture_output=True, text=True, check=True, timeout=60)
    data = json.loads(result.stdout)
    stream = next((s for s in data['streams'] if s['codec_type'] == 'video'), None)
    if not stream:
        raise ValueError('素材中没有视频轨道')
    fps = Fraction(stream.get('avg_frame_rate', '0/1'))
    duration = float(stream.get('duration') or data['format'].get('duration', 0))
    if not 1 <= fps <= 120 or not 0 < duration <= 120:
        raise ValueError('视频超分支持最长 120 秒、1–120 fps 的视频')
    frames = int(stream.get('nb_frames') or round(duration * fps))
    return {'width': stream['width'], 'height': stream['height'], 'fps': fps, 'frames': frames,
            'duration': duration, 'audio': any(s['codec_type'] == 'audio' for s in data['streams'])}


def cli_arguments(source, destination, model, width, height):
    if model not in MODEL_PRESETS:
        raise ValueError('请选择支持的视频超分模型')
    return [str(ROOT / 'video-sr-venv/bin/python'), str(ROOT / 'SeedVR2/inference_cli.py'), str(source),
            '--output', str(destination), '--output_format', 'mp4', '--video_backend', 'ffmpeg',
            '--model_dir', str(ROOT / 'ComfyUI/models/SEEDVR2'), '--dit_model', MODEL_PRESETS[model],
            '--resolution', str(min(width, height)), '--max_resolution', str(max(width, height)),
            '--batch_size', '5', '--chunk_size', '33', '--temporal_overlap', '4', '--uniform_batch_size',
            '--color_correction', 'lab', '--attention_mode', 'sdpa',
            '--dit_offload_device', 'cpu', '--vae_offload_device', 'cpu',
            '--vae_encode_tiled', '--vae_decode_tiled', '--seed', '42', '--cache_dit', '--cache_vae']


def run_inference(arguments, log_path, interrupted):
    with log_path.open('wb') as log:
        process = subprocess.Popen(arguments, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                                   env={**os.environ, 'PYTHONUNBUFFERED': '1', 'HF_HUB_OFFLINE': '1'})
        try:
            deadline = time.monotonic() + 7200
            while process.poll() is None:
                interrupted()
                if time.monotonic() > deadline:
                    raise TimeoutError('视频超分运行超过两小时')
                time.sleep(.5)
            if process.returncode:
                raise RuntimeError('SeedVR2 视频超分失败：' + log_path.read_text(errors='replace')[-3500:])
        finally:
            if process.poll() is None:
                # WARN: Interrupt the entire CLI process group so cancellation releases its GPU workers.
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()


def finish_video(enhanced, source, destination, info, size):
    subprocess.run(['ffmpeg', '-y', '-v', 'error', '-i', str(enhanced), '-i', str(source),
                    '-map', '0:v:0', '-map', '1:a?', '-vf', f'scale={size[0]}:{size[1]}:flags=lanczos,setsar=1',
                    '-r', str(info['fps']), '-fps_mode', 'cfr', '-frames:v', str(info['frames']),
                    '-t', str(float(info['frames'] / info['fps'])), '-c:v', 'libx264', '-crf', '16',
                    '-preset', 'fast', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '192k',
                    '-movflags', '+faststart', str(destination)], check=True, capture_output=True, timeout=600)
    output = probe_video(destination)
    if ((output['width'], output['height']) != size or output['frames'] != info['frames']
            or abs(float(output['fps'] - info['fps'])) > .001 or output['audio'] != info['audio']
            or abs(output['duration'] - info['frames'] / info['fps']) > .1):
        raise RuntimeError('超分成片的尺寸、帧数、帧率或音轨校验失败')


class WorkbenchVideoUpscale:
    @classmethod
    def INPUT_TYPES(cls):
        return {'required': {'video': ('VIDEO',), 'model': (list(MODEL_PRESETS),),
                             'scale': ('INT', {'default': 2, 'min': 2, 'max': 4, 'step': 2})}}

    RETURN_TYPES = ('VIDEO',)
    FUNCTION = 'execute'
    CATEGORY = 'workbench/video'

    def execute(self, video, model, scale):
        import folder_paths
        from comfy import model_management
        from comfy_api.latest import InputImpl, Types

        destination = Path(folder_paths.get_temp_directory()) / ('sr-' + uuid.uuid4().hex + '.mp4')
        try:
            with tempfile.TemporaryDirectory(prefix='workbench-sr-') as directory:
                directory = Path(directory)
                source, enhanced = directory / 'source.mp4', directory / 'enhanced.mp4'
                video.save_to(str(source), format=Types.VideoContainer('mp4'), codec=Types.VideoCodec('h264'))
                info = probe_video(source)
                size = output_dimensions(info['width'], info['height'], scale)
                if size[0] <= info['width'] and size[1] <= info['height']:
                    raise ValueError('输入视频已达到 4K 上限，无需继续放大')
                model_management.unload_all_models()
                model_management.soft_empty_cache()
                run_inference(cli_arguments(source, enhanced, model, *size), directory / 'inference.log',
                              model_management.throw_exception_if_processing_interrupted)
                model_management.throw_exception_if_processing_interrupted()
                if not enhanced.is_file():
                    raise RuntimeError('SeedVR2 没有输出成片')
                finish_video(enhanced, source, destination, info, size)
            return (InputImpl.VideoFromFile(str(destination)),)
        except BaseException:
            destination.unlink(missing_ok=True)
            raise


NODE_CLASS_MAPPINGS = {'WorkbenchVideoUpscale': WorkbenchVideoUpscale}
NODE_DISPLAY_NAME_MAPPINGS = {'WorkbenchVideoUpscale': '视频超分 · SeedVR2'}
