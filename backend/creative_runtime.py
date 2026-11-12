"""Media preparation and export for compiled creative jobs."""
import asyncio
import io
import os
import shutil
import subprocess
import time
import uuid

import aiohttp
from PIL import Image, ImageOps

from .assets import asset_path, inspect_media


def reference_png(path):
    with Image.open(path) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
        image.thumbnail((4096, 4096), Image.Resampling.LANCZOS)
        output = io.BytesIO()
        image.save(output, "PNG")
        return output.getvalue()


async def upload_references(manager, job):
    for key in job["creative"]["asset_ids"]:
        path, _ = asset_path(manager.store, key)
        content = await asyncio.to_thread(reference_png, path)
        filename = f"creative-{key}.png"
        form = aiohttp.FormData()
        form.add_field("image", content, filename=filename, content_type="image/png")
        form.add_field("type", "input")
        form.add_field("overwrite", "true")
        port = manager.machine(job["account"])["port"]
        async with manager.http.post(f"http://127.0.0.1:{port}/upload/image", data=form,
                                     timeout=aiohttp.ClientTimeout(total=120)) as response:
            response.raise_for_status()
            uploaded = await response.json()
            if uploaded.get("name") != filename or uploaded.get("subfolder", ""):
                raise RuntimeError("参考素材上传后名称不匹配，已停止提交")


async def command(*args):
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        _, error = await asyncio.wait_for(process.communicate(), 600)
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise
    if process.returncode:
        raise RuntimeError("视频导出失败：" + error.decode(errors="replace")[-1800:])


async def export_video(path, settings):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("视频导出需要 ffmpeg，请安装后恢复任务")
    duration, fps = settings["duration"], settings["fps"]
    frames = round(duration * fps)
    exported, first = path.with_name(path.stem + ".export.mp4"), path.with_name(path.stem + ".first.png")
    try:
        args = [ffmpeg, "-y", "-v", "error", "-i", str(path)]
        video_filter = f"fps={fps},trim=end_frame={frames},settb=1/{fps},setpts=N,format=yuv420p"
        if settings.get("loop"):
            await command(ffmpeg, "-y", "-v", "error", "-i", str(path), "-frames:v", "1", str(first))
            args += ["-loop", "1", "-framerate", str(fps), "-i", str(first)]
            blend_frames = max(1, round(fps * .25))
            alpha = f"min(1,max(0,(N-{frames - blend_frames})/{blend_frames}))"
            # WARN: Both blend inputs must share a clock; mixed time bases can freeze motion before the tail.
            filters = f"[0:v]{video_filter}[moving];[1:v]{video_filter}[still];[moving][still]blend=all_expr='A*(1-{alpha})+B*{alpha}':shortest=1[v]"
            args += ["-filter_complex", filters, "-map", "[v]"]
        else:
            args += ["-vf", video_filter, "-map", "0:v:0"]
        args += ["-map", "0:a?", "-af", "apad", "-t", str(duration), "-frames:v", str(frames),
                 "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p",
                 "-c:a", "aac", "-movflags", "+faststart", str(exported)]
        await command(*args)
        info = await inspect_media(exported)
        if abs(info.get("duration", 0) - duration) > .1:
            raise RuntimeError("视频导出时长校验失败，保留原始成片以便恢复")
        return exported
    except BaseException:
        exported.unlink(missing_ok=True)
        raise
    finally:
        first.unlink(missing_ok=True)


async def register_result(store, path, job_id, node_id):
    key = uuid.uuid5(uuid.NAMESPACE_URL, f"creative:{job_id}:{node_id}:{path.name}").hex
    existing = store.get("assets", key)
    if existing:
        return existing
    info = await inspect_media(path)
    directory = store.directory / "assets"
    directory.mkdir(exist_ok=True)
    await asyncio.to_thread(shutil.copyfile, path, directory / (key + info["extension"]))
    return store.put("assets", key, {"id": key, "name": path.name, "bytes": path.stat().st_size,
                                     "created_at": time.time(), "job_id": job_id, "node_id": node_id, **info})
