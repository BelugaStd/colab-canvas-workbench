"""Executed on the owned Colab VM. Contains no local credentials."""
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

if __package__:
    from . import minimax_full_remote, video_sr_remote
else:
    import minimax_full_remote
    import video_sr_remote

ROOT = Path("/content/colab-workbench")
COMFY = ROOT / "ComfyUI"
COMMIT = "4d64a8bc072b81c9b47321789b4d83fb7348ae2a"
REVISION = "54472221d27c695fca9621b0c3ed52f06c4af707"
FILES = [
    "diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors",
    "text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
    "vae/minimax_h3_video_vae_int8_convrot.safetensors",
    "vae/minimax_h3_audio_vae_fp32.safetensors",
]
IMAGE_REVISION = "0bb149f51e3156ed4b90dd45d5620346bfe1df8f"
MOSS_REVISION = "2929efe3cdd506c0a430bcfca70a9a8d805cc095"
QWEN_EDIT_REVISION = "f68ace85e60b4a02a323e394253731947657b7d2"
QWEN_SHARED_REVISION = "1f12b17be14c89b026c51a91d67c32f84bb047bc"
MODEL_GROUPS = {
    "minimax-h3": [("Comfy-Org/MiniMax-H3", REVISION, file) for file in FILES],
    "minimax-h3-reference": [("Comfy-Org/MiniMax-H3", REVISION, file) for file in
                             ['diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors', *FILES[1:]]],
    "minimax-image": [
        ("Comfy-Org/MiniMax-H3-Image", IMAGE_REVISION, "diffusion_models/minimax_h3_image_pruned_int8_convrot.safetensors"),
        ("Comfy-Org/MiniMax-H3-Image", IMAGE_REVISION, "vae/minimax_h3_image_vae_fp16.safetensors"),
        ("Comfy-Org/MiniMax-H3", REVISION, FILES[1]),
    ],
    'minimax-image-full': [(minimax_full_remote.REPOSITORY, minimax_full_remote.REVISION, file)
                           for file in minimax_full_remote.FILES] + [
        ('Comfy-Org/MiniMax-H3', REVISION, 'text_encoders/qwen3vl_32b_minimax_h3_bf16.safetensors'),
        ('Comfy-Org/MiniMax-H3-Image', IMAGE_REVISION, 'vae/minimax_h3_image_vae_fp16.safetensors'),
    ],
    "moss-image": [("Comfy-Org/MOSS-Image", MOSS_REVISION, file) for file in (
        "diffusion_models/moss_image_sft_int8_convrot.safetensors",
        "text_encoders/qwen3.5_9b_w4a8_gptq.safetensors", "vae/flux2-vae_small.safetensors")],
    "moss-edit": [("Comfy-Org/MOSS-Image", MOSS_REVISION, "loras/moss_image_edit_lora_rank_64_bf16.safetensors")],
    "qwen-edit": [
        ("Comfy-Org/Qwen-Image-Edit_ComfyUI", QWEN_EDIT_REVISION, "split_files/diffusion_models/qwen_image_edit_2511_int8_convrot.safetensors"),
        ("Comfy-Org/Qwen-Image_ComfyUI", QWEN_SHARED_REVISION, "split_files/text_encoders/qwen_2.5_vl_7b_fp8_scaled.safetensors"),
        ("Comfy-Org/Qwen-Image_ComfyUI", QWEN_SHARED_REVISION, "split_files/vae/qwen_image_vae.safetensors"),
    ],
    "upscale": [],
}
MODEL_GROUPS.update({name: [(video_sr_remote.WEIGHTS_REPOSITORY, video_sr_remote.WEIGHTS_REVISION, file)
                           for file in (filename, video_sr_remote.VAE_FILE)]
                     for name, filename in video_sr_remote.MODEL_PRESETS.items()})
UPSCALER_URL = "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth"
UPSCALER_SHA256 = "4fa0d38905f75ac06eb49a7951b426670021be3018265fd191d2125df9d682f1"
STATUS_LOCK = threading.Lock()


def selected_models():
    names = sys.argv[sys.argv.index("--models") + 1].split(",") if "--models" in sys.argv else ["minimax-h3"]
    if not names or any(name not in MODEL_GROUPS for name in names):
        raise ValueError("Unsupported model preset")
    return names


def model_destination(repo, filename):
    if repo == minimax_full_remote.REPOSITORY:
        return 'minimax-image-full/' + filename
    return 'SEEDVR2/' + filename if repo == video_sr_remote.WEIGHTS_REPOSITORY else filename.removeprefix('split_files/')


def models_ready(root, names):
    try:
        records = {r['file']: r for r in json.loads((root / 'assets.json').read_text())}
        for repo, revision, filename in {file for name in names for file in MODEL_GROUPS[name]}:
            destination = model_destination(repo, filename)
            record = records.get(destination, {})
            path = root / 'ComfyUI/models' / destination
            if record.get('revision') != revision or not path.is_file() or not path.stat().st_size == record.get('bytes', 0) > 0:
                return False
        if 'upscale' in names:
            path = root / 'ComfyUI/models/upscale_models/RealESRGAN_x4plus.pth'
            if records.get('upscale_models/RealESRGAN_x4plus.pth', {}).get('source') != UPSCALER_URL or not path.is_file() or path.stat().st_size != 67040989:
                return False
        if any(name in video_sr_remote.MODEL_PRESETS for name in names):
            if ((root / 'video-sr-ready').read_text() != video_sr_remote.CODE_REVISION
                    or not (root / 'video-sr-venv/bin/python').is_file()):
                return False
        return True
    except (OSError, ValueError, TypeError, KeyError):
        return False


def status(phase, message, **extra):
    data = {"phase": phase, "message": message, "time": time.time(), **extra}
    name = os.environ.get("WB_DEPLOY_STATUS", "deploy-status")
    with STATUS_LOCK:
        temp = ROOT / (name + ".tmp")
        temp.write_text(json.dumps(data))
        temp.replace(ROOT / (name + ".json"))
        print(json.dumps(data), flush=True)


def run(args):
    subprocess.run(args, check=True)


def install_packages(python, requirements):
    run([str(python), '-m', 'pip', 'install', '--disable-pip-version-check', '--prefer-binary',
         '--cache-dir', str(ROOT / 'package-cache'), '-r', str(requirements)])


def api_requirements():
    excluded = ('comfyui-frontend-package', 'comfyui-workflow-templates', 'comfyui-embedded-docs')
    text = '\n'.join(line for line in (COMFY / 'requirements.txt').read_text().splitlines()
                     if not line.strip().startswith(excluded)) + '\n'
    path = ROOT / 'comfy-api-requirements.txt'
    path.write_text(text)
    return path


def prepare_environment(python):
    requirements = api_requirements()
    signature = hashlib.sha256(requirements.read_bytes() + COMMIT.encode() + sys.version.encode()).hexdigest()
    marker = ROOT / 'comfy-environment-ready'
    with ThreadPoolExecutor(max_workers=1) as pool:
        download = pool.submit(download_models)
        if not marker.exists() or marker.read_text() != signature:
            status('installing', '并行安装 ComfyUI 依赖与下载模型')
            try:
                install_packages(python, requirements)
                marker.write_text(signature)
            except BaseException as exc:
                status('error', f'依赖安装失败：{exc}')
                raise
        return download.result()


def healthy():
    try:
        with urllib.request.urlopen("http://127.0.0.1:8188/system_stats", timeout=3) as response:
            return json.load(response)
    except Exception:
        return None


def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    # A single deployment owns this directory; retry does not race a previous download.
    import fcntl
    with (ROOT / "deploy.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | (0 if "--models-only" in sys.argv else fcntl.LOCK_NB))
        except BlockingIOError:
            print("Deployment is already active", flush=True)
            return
        try:
            if "--models-only" in sys.argv:
                download_models()
                status("ready", "所需模型已就绪")
                return
            if healthy():
                download_models()
                status("ready", "ComfyUI 已就绪")
                return
            status("checking", "检查 Blackwell GPU 与 CUDA")
            import torch
            if not torch.cuda.is_available() or torch.cuda.get_device_capability()[0] < 12:
                raise RuntimeError("此预设需要 Blackwell GPU：请申请 G4 / RTX PRO 6000")
            if int(torch.version.cuda.split('.')[0]) < 13:
                raise RuntimeError("INT8 ConvRot 预设需要 CUDA 13 的 PyTorch")
            status("installing", "安装已验证版本的 ComfyUI")
            # Checkout first; downloads must not create models/ before Git initialization.
            COMFY.mkdir(parents=True, exist_ok=True)
            if not (COMFY / ".git").exists():
                run(["git", "-C", str(COMFY), "init"])
                run(["git", "-C", str(COMFY), "remote", "add", "origin", "https://github.com/Comfy-Org/ComfyUI.git"])
            head = subprocess.run(['git', '-C', str(COMFY), 'rev-parse', 'HEAD'], capture_output=True, text=True)
            if head.returncode or head.stdout.strip() != COMMIT:
                run(["git", "-C", str(COMFY), "fetch", "--depth", "1", "origin", COMMIT])
                run(["git", "-C", str(COMFY), "checkout", "--detach", COMMIT])
            python = ROOT / "venv/bin/python"
            if not python.exists():
                (ROOT / 'comfy-environment-ready').unlink(missing_ok=True)
                run([sys.executable, "-m", "venv", "--without-pip", "--system-site-packages", str(ROOT / "venv")])
            tools = [package for package in ('huggingface_hub', 'hf_xet') if importlib.util.find_spec(package) is None]
            if tools:
                run([sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check',
                     '--cache-dir', str(ROOT / 'package-cache'), *tools])
            prepare_environment(python)
            run([str(python), str(ROOT / "deploy_remote.py"), "--serve", "--models", ",".join(selected_models())])
        except Exception as exc:
            traceback.print_exc()
            status("error", str(exc))
            raise


def download_models():
    names = selected_models()
    if models_ready(ROOT, names):
        status('checking', '复用已校验的固定版本模型，无需重复下载')
        return json.loads((ROOT / 'assets.json').read_text())
    os.environ.setdefault('HF_XET_HIGH_PERFORMANCE', '0')
    os.environ.setdefault('HF_XET_CLIENT_AC_INITIAL_DOWNLOAD_CONCURRENCY', '4')
    os.environ.setdefault('HF_XET_CLIENT_AC_MAX_DOWNLOAD_CONCURRENCY', '16')
    from huggingface_hub import hf_hub_download
    files = list(dict.fromkeys(file for name in names for file in MODEL_GROUPS[name]))
    started = time.monotonic()
    records = []

    def fetch(file):
        repo, revision, filename = file
        options = {'revision': revision, 'cache_dir': str(ROOT / 'hf-cache')}
        try:
            path = Path(hf_hub_download(repo, filename, local_files_only=True, **options))
        except Exception:
            for attempt in range(6):
                try:
                    path = Path(hf_hub_download(repo, filename, **options))
                    break
                except Exception as exc:
                    response = getattr(exc, 'response', None)
                    code = getattr(response, 'status_code', None)
                    retryable = code in {408, 429, 500, 502, 503, 504}
                    if code is None:
                        retryable = bool(re.search(r'\b(?:408|429|500|502|503|504)\b|timed? ?out|connection|network|unexpected.eof|incomplete', str(exc), re.I))
                    if not retryable or attempt == 5:
                        raise RuntimeError(f'模型下载失败 {filename}：{exc}') from exc
                    delay = min(60, 5 * 2**attempt)
                    status('downloading', f'{filename} 下载暂时中断，{delay} 秒后从缓存继续', retry=attempt + 1)
                    time.sleep(delay)
        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError(f'模型文件为空或无效：{filename}')
        destination = model_destination(repo, filename)
        target = COMFY / "models" / destination
        target.parent.mkdir(parents=True, exist_ok=True)
        resolved = path.resolve(strict=True)
        if not target.exists() or target.resolve() != resolved:
            temporary = target.with_suffix('.link')
            temporary.unlink(missing_ok=True)
            temporary.symlink_to(resolved)
            temporary.replace(target)
        return {"file": destination, "bytes": path.stat().st_size, "revision": revision}

    workers = max(1, min(8, int(os.environ.get('WB_DOWNLOAD_WORKERS', '4'))))
    total = len(files) + int('upscale' in names)
    needs_runtime = any(name in video_sr_remote.MODEL_PRESETS for name in names)
    with ThreadPoolExecutor(max_workers=workers) as pool, ThreadPoolExecutor(max_workers=1) as runtime_pool:
        runtime = runtime_pool.submit(video_sr_remote.prepare_runtime, status, install_packages) if needs_runtime else None
        futures = {pool.submit(fetch, file): file[2] for file in files}
        if 'upscale' in names:
            futures[pool.submit(download_upscaler)] = 'RealESRGAN_x4plus.pth'
        status('downloading', f'并发准备模型 · 0/{total}', total_files=total, completed_files=0, workers=workers)
        try:
            for future in as_completed(futures):
                records.append(future.result())
                status('downloading', f'模型已就绪 {len(records)}/{total} · {futures[future]}',
                       total_files=total, completed_files=len(records), workers=workers,
                       elapsed_seconds=round(time.monotonic() - started, 2))
            if runtime:
                runtime.result()
        except BaseException:
            for future in futures:
                future.cancel()
            if runtime:
                runtime.cancel()
            raise
    records.sort(key=lambda record: record['file'])
    try:
        previous = {r['file']: r for r in json.loads((ROOT / 'assets.json').read_text())}
    except (OSError, ValueError, KeyError, TypeError):
        previous = {}
    previous.update({r['file']: r for r in records})
    temporary = ROOT / 'assets.tmp'
    temporary.write_text(json.dumps(list(previous.values())))
    temporary.replace(ROOT / 'assets.json')
    return records


def download_upscaler():
    target = COMFY / "models/upscale_models/RealESRGAN_x4plus.pth"
    target.parent.mkdir(parents=True, exist_ok=True)
    valid = False
    if target.exists():
        with target.open("rb") as stream:
            valid = hashlib.file_digest(stream, "sha256").hexdigest() == UPSCALER_SHA256
    if not valid:
        status("downloading", "下载 Real-ESRGAN 超分模型")
        temporary = target.with_suffix(".part")
        try:
            size = 67040989
            with temporary.open("wb") as stream:
                for start in range(0, size, 4 * 1024 * 1024):
                    end = min(start + 4 * 1024 * 1024, size) - 1
                    for attempt in range(3):
                        request = urllib.request.Request(UPSCALER_URL, headers={"Range": f"bytes={start}-{end}"})
                        with urllib.request.urlopen(request, timeout=60) as response:
                            chunk = response.read()
                            if response.status == 206 and len(chunk) == end - start + 1:
                                stream.write(chunk)
                                break
                    else:
                        raise RuntimeError("Real-ESRGAN 下载中断，请重试")
            with temporary.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != UPSCALER_SHA256:
                    raise RuntimeError("Real-ESRGAN SHA256 校验失败")
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
    return {"file": "upscale_models/RealESRGAN_x4plus.pth", "bytes": target.stat().st_size, "source": UPSCALER_URL}


def download_and_serve(prepare=True):
    records = download_models() if prepare else json.loads((ROOT / 'assets.json').read_text())
    node = COMFY / "custom_nodes/workbench_video_sr/__init__.py"
    node.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path(video_sr_remote.__file__), node)
    image_node = COMFY / 'custom_nodes/workbench_minimax_full/__init__.py'
    image_node.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path(minimax_full_remote.__file__), image_node)
    status("starting", "启动 ComfyUI，检查模型加载器")
    frontend = ROOT / 'api-frontend'
    frontend.mkdir(exist_ok=True)
    (frontend / 'index.html').write_text('<!doctype html><title>Workbench API</title>')
    with (ROOT / "server.log").open("ab") as log:
        proc = subprocess.Popen([
            sys.executable, str(COMFY / "main.py"), "--listen", "127.0.0.1", "--port", "8188",
            "--disable-auto-launch", "--disable-all-custom-nodes", "--whitelist-custom-nodes", "workbench_video_sr", 'workbench_minimax_full',
            '--front-end-root', str(frontend), '--disable-partner-nodes',
            "--use-pytorch-cross-attention",
            "--preview-method", "none",
        ], cwd=COMFY, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    (ROOT / "server.pid").write_text(str(proc.pid))
    for _ in range(180):
        if proc.poll() is not None:
            raise RuntimeError("ComfyUI 启动失败：" + (ROOT / "server.log").read_text(errors="replace")[-3500:])
        if healthy():
            status("ready", "ComfyUI 与所需模型已就绪", gpu="RTX PRO 6000", assets=records)
            return
        time.sleep(2)
    raise TimeoutError("ComfyUI 启动超时，请检查 server.log")


if __name__ == "__main__":
    os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
    if "--download-and-serve" in sys.argv:
        download_and_serve()
    elif '--serve' in sys.argv:
        download_and_serve(prepare=False)
    else:
        main()
