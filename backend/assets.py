"""Validated local media, addressed by opaque IDs rather than caller file paths."""
import asyncio
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from urllib.parse import unquote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from PIL import Image, UnidentifiedImageError

MAX_BYTES = 128 * 1024 * 1024
Image.MAX_IMAGE_PIXELS = None
router = APIRouter(prefix="/api/assets")


def image_info(path):
    with Image.open(path) as image:
        if image.format not in {"PNG", "JPEG", "WEBP"}:
            raise ValueError("图片只支持 PNG、JPEG 和 WebP")
        width, height = image.size
        orientation = image.getexif().get(274,1)
        if orientation in {5,6,7,8}:
            width,height = height,width
        ext, media_type = {"PNG": (".png", "image/png"), "JPEG": (".jpg", "image/jpeg"),
                           "WEBP": (".webp", "image/webp")}[image.format]
    with Image.open(path) as validation:
        validation.verify()
    return {"extension": ext, "media_type": media_type, "width": width, "height": height, "orientation":orientation}


async def inspect_media(path):
    try:
        return await asyncio.to_thread(image_info, path)
    except UnidentifiedImageError:
        pass
    except (OSError, SyntaxError, Image.DecompressionBombError) as exc:
        raise ValueError("图片内容损坏或尺寸过大") from exc
    probe = shutil.which("ffprobe")
    if not probe:
        raise ValueError("此文件不是有效图片；视频和音频上传需要安装 ffmpeg")
    process = await asyncio.create_subprocess_exec(
        probe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), 15)
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise
    if process.returncode:
        raise ValueError("文件内容无效；支持 PNG、JPEG、WebP、MP4、WebM、MP3、WAV")
    data = json.loads(stdout)
    formats = data.get("format", {}).get("format_name", "").split(",")
    streams = data.get("streams", [])
    if "mp4" in formats:
        ext, media_type = ".mp4", "video/mp4"
    elif "webm" in formats:
        ext, media_type = ".webm", "video/webm"
    elif "mp3" in formats:
        ext, media_type = ".mp3", "audio/mpeg"
    elif "wav" in formats:
        ext, media_type = ".wav", "audio/wav"
    else:
        raise ValueError("视频只支持 MP4 / WebM，音频只支持 MP3 / WAV")
    if not any(s.get("codec_type") in {"video", "audio"} for s in streams):
        raise ValueError("文件中没有可播放的音视频轨道")
    result = {"extension": ext, "media_type": media_type,
              "duration": float(data.get("format", {}).get("duration", 0))}
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    if video:
        result.update(width=video.get("width"), height=video.get("height"))
    return result


def asset_path(store, key):
    if not re.fullmatch(r"[0-9a-f]{32}", key):
        raise HTTPException(404, "素材不存在")
    asset = store.get("assets", key)
    if not asset:
        raise HTTPException(404, "素材不存在")
    path = store.directory / "assets" / (key + asset["extension"])
    if not path.is_file():
        raise HTTPException(404, "素材文件不存在")
    return path, asset


@router.post("", status_code=201)
async def upload_asset(request: Request):
    if int(request.headers.get("content-length", "0")) > MAX_BYTES:
        raise HTTPException(413, "素材不能超过 128 MB")
    store, key = request.app.state.store, uuid.uuid4().hex
    directory = store.directory / "assets"
    directory.mkdir(exist_ok=True)
    temporary = directory / (key + ".upload")
    try:
        size = 0
        with temporary.open("xb") as stream:
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_BYTES:
                    raise HTTPException(413, "素材不能超过 128 MB")
                stream.write(chunk)
        if not size:
            raise ValueError("不能上传空文件")
        info = await inspect_media(temporary)
        name = Path(unquote(request.headers.get("x-filename", "素材")).replace("\\", "/")).name[:100] or "素材"
        temporary.replace(directory / (key + info["extension"]))
        return store.put("assets", key, {"id": key, "name": name, "bytes": size, "created_at": time.time(), **info})
    finally:
        temporary.unlink(missing_ok=True)


@router.get("")
async def list_assets(request: Request):
    return sorted(request.app.state.store.all("assets"), key=lambda a: a["created_at"], reverse=True)


@router.get("/{key}/file")
async def read_asset(key: str, request: Request):
    path, asset = asset_path(request.app.state.store, key)
    return FileResponse(path, media_type=asset["media_type"], filename=asset["name"], content_disposition_type="inline")
