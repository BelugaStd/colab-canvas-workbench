"""Executed on the owned Colab VM. Contains no local credentials."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
import urllib.request
from pathlib import Path

if __package__:
    from . import video_sr_remote
else:
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
MODEL_GROUPS = {
    "minimax-h3": [("Comfy-Org/MiniMax-H3", REVISION, file) for file in FILES],
    "minimax-h3-reference": [("Comfy-Org/MiniMax-H3", REVISION, file) for file in
                             ['diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors', *FILES[1:]]],
    "minimax-image": [
        ("Comfy-Org/MiniMax-H3-Image", IMAGE_REVISION, "diffusion_models/minimax_h3_image_pruned_int8_convrot.safetensors"),
        ("Comfy-Org/MiniMax-H3-Image", IMAGE_REVISION, "vae/minimax_h3_image_vae_fp16.safetensors"),
        ("Comfy-Org/MiniMax-H3", REVISION, FILES[1]),
    ],
    "moss-image": [("Comfy-Org/MOSS-Image", MOSS_REVISION, file) for file in (
        "diffusion_models/moss_image_sft_int8_convrot.safetensors",
        "text_encoders/qwen3.5_9b_w4a8_gptq.safetensors", "vae/flux2-vae_small.safetensors")],
    "moss-edit": [("Comfy-Org/MOSS-Image", MOSS_REVISION, "loras/moss_image_edit_lora_rank_64_bf16.safetensors")],
    "upscale": [],
}
MODEL_GROUPS.update({name: [(video_sr_remote.WEIGHTS_REPOSITORY, video_sr_remote.WEIGHTS_REVISION, file)
                           for file in (filename, video_sr_remote.VAE_FILE)]
                     for name, filename in video_sr_remote.MODEL_PRESETS.items()})
UPSCALER_URL = "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth"
UPSCALER_SHA256 = "4fa0d38905f75ac06eb49a7951b426670021be3018265fd191d2125df9d682f1"


def selected_models():
    names = sys.argv[sys.argv.index("--models") + 1].split(",") if "--models" in sys.argv else ["minimax-h3"]
    if not names or any(name not in MODEL_GROUPS for name in names):
        raise ValueError("Unsupported model preset")
    return names


def status(phase, message, **extra):
    data = {"phase": phase, "message": message, "time": time.time(), **extra}
    name = os.environ.get("WB_DEPLOY_STATUS", "deploy-status")
    temp = ROOT / (name + ".tmp")
    temp.write_text(json.dumps(data))
    temp.replace(ROOT / (name + ".json"))
    print(json.dumps(data), flush=True)


def run(args):
    subprocess.run(args, check=True)


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
            run(["git", "-C", str(COMFY), "fetch", "--depth", "1", "origin", COMMIT])
            run(["git", "-C", str(COMFY), "checkout", "--detach", COMMIT])
            python = ROOT / "venv/bin/python"
            if not python.exists():
                run([sys.executable, "-m", "venv", "--without-pip", "--system-site-packages", str(ROOT / "venv")])
            run([str(python), "-m", "pip", "install", "--no-cache-dir", "--disable-pip-version-check", "-r", str(COMFY / "requirements.txt"), "huggingface_hub"])
            # Re-exec into the venv for downloads and serving after the install lock is released.
            run([str(python), str(ROOT / "deploy_remote.py"), "--download-and-serve", "--models", ",".join(selected_models())])
        except Exception as exc:
            traceback.print_exc()
            status("error", str(exc))
            raise


def download_models():
    from huggingface_hub import hf_hub_download
    names = selected_models()
    if any(name in video_sr_remote.MODEL_PRESETS for name in names):
        video_sr_remote.prepare_runtime(status)
    files = list(dict.fromkeys(file for name in names for file in MODEL_GROUPS[name]))
    records = []
    for index, (repo, revision, filename) in enumerate(files):
        status("downloading", f"准备模型 {index + 1}/{len(files)} · {Path(filename).name}", file_index=index, total_files=len(files))
        path = Path(hf_hub_download(repo, filename, revision=revision, cache_dir=str(ROOT / "hf-cache")))
        destination = "SEEDVR2/" + filename if repo == video_sr_remote.WEIGHTS_REPOSITORY else filename
        target = COMFY / "models" / destination
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            if target.is_symlink():
                target.unlink()
            target.symlink_to(path.resolve(strict=True))
        records.append({"file": destination, "bytes": path.stat().st_size, "revision": revision})
    if "upscale" in names:
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
        records.append({"file": "upscale_models/RealESRGAN_x4plus.pth", "bytes": target.stat().st_size, "source": UPSCALER_URL})
    (ROOT / "assets.json").write_text(json.dumps(records))
    return records


def download_and_serve():
    records = download_models()
    node = COMFY / "custom_nodes/workbench_video_sr/__init__.py"
    node.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path(video_sr_remote.__file__), node)
    status("starting", "启动 ComfyUI，检查模型加载器")
    with (ROOT / "server.log").open("ab") as log:
        proc = subprocess.Popen([
            sys.executable, str(COMFY / "main.py"), "--listen", "127.0.0.1", "--port", "8188",
            "--disable-auto-launch", "--disable-all-custom-nodes", "--whitelist-custom-nodes", "workbench_video_sr",
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
    else:
        main()
