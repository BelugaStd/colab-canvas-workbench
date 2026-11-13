"""Deterministic local media actions over validated asset IDs."""
import asyncio
import json
import math
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from fractions import Fraction
from pathlib import Path

from .assets import asset_path, inspect_media


def number(value, name, minimum, maximum):
    if type(value) not in (int, float) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f'{name} 必须介于 {minimum} 和 {maximum}')
    return value


async def run_command(arguments, timeout=1200):
    process = await asyncio.create_subprocess_exec(*map(str, arguments),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise
    if process.returncode:
        raise RuntimeError('媒体处理失败：' + stderr.decode(errors='replace')[-2500:])
    return stdout


def binary(name):
    executable = shutil.which(name)
    if not executable:
        raise RuntimeError('媒体工具需要安装 ffmpeg 与 ffprobe')
    return executable


async def probe(path):
    return json.loads(await run_command([binary('ffprobe'), '-v', 'error', '-show_streams',
                                        '-show_format', '-of', 'json', path], timeout=30))


async def register(store, path, name, metadata=None, result_id=None):
    if not isinstance(name, str) or not 1 <= len(name) <= 100:
        raise ValueError('素材名称必须为 1–100 个字符')
    info = await inspect_media(path)
    key = result_id or uuid.uuid4().hex
    existing = store.get('assets', key)
    if existing:
        asset_path(store, key)
        return existing
    folder = store.directory / 'assets'
    folder.mkdir(exist_ok=True)
    target = folder / (key + info['extension'])
    copying = asyncio.create_task(asyncio.to_thread(shutil.copyfile, path, target))
    try:
        await asyncio.shield(copying)
        return store.put('assets', key, {'id': key, 'name': name, 'bytes': target.stat().st_size,
            'created_at': time.time(), **info, **(metadata or {})})
    except BaseException:
        try:
            await copying
        except Exception:
            pass
        target.unlink(missing_ok=True)
        raise


async def extract(store, asset_id, time_seconds=0, label='提取帧', result_id=None):
    if result_id and store.get('assets', result_id):
        asset_path(store, result_id)
        return store.get('assets', result_id)
    source, asset = asset_path(store, asset_id)
    if not asset['media_type'].startswith('video/'):
        raise ValueError('提取帧需要视频素材')
    info = await probe(source)
    duration = float(info['format']['duration'])
    number(time_seconds, '帧时间', 0, max(0, duration - .001))
    directory = store.directory / 'agent-media'
    directory.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='frame-', dir=directory) as temporary:
        output = Path(temporary) / 'frame.png'
        await run_command([binary('ffmpeg'), '-v', 'error', '-i', source, '-ss', time_seconds,
                           '-map', '0:v:0', '-frames:v', '1', '-update', '1', output], timeout=120)
        if not output.is_file():
            raise RuntimeError('视频中没有该时间点的画面')
        return await register(store, output, label, {'agent_source': {'asset_id': asset_id, 'time_seconds': time_seconds}}, result_id)


async def assemble(store, clips, name='成片', fps=24, width=1280, height=720, result_id=None):
    if result_id and store.get('assets', result_id):
        asset_path(store, result_id)
        return store.get('assets', result_id)
    if not isinstance(clips, list) or not 1 <= len(clips) <= 60:
        raise ValueError('成片需包含 1–60 段视频')
    if type(fps) is not int or fps not in (24, 30, 60):
        raise ValueError('导出帧率可选 24 / 30 / 60')
    if any(type(value) is not int or value % 2 or not 32 <= value <= 3840 for value in (width, height)):
        raise ValueError('导出宽高必须为 32–3840 范围内的偶数')
    if width * height > 3840 * 2160:
        raise ValueError('导出分辨率不能超过 4K 像素面积')
    if not isinstance(name, str) or not 1 <= len(name) <= 100:
        raise ValueError('成片名称必须为 1–100 个字符')
    prepared, total_frames = [], 0
    for clip in clips:
        if not isinstance(clip, dict) or set(clip) - {'asset_id', 'start', 'duration'}:
            raise ValueError('视频片段只支持 asset_id、start 和 duration')
        source, asset = asset_path(store, clip.get('asset_id', ''))
        if not asset['media_type'].startswith('video/'):
            raise ValueError('成片输入必须为视频素材')
        info = await probe(source)
        actual = float(info['format']['duration'])
        start = number(clip.get('start', 0), '片段起点', 0, max(0, actual - 1 / fps))
        duration = number(clip.get('duration', actual - start), '片段时长', 1 / fps, 600)
        if start + duration > actual + .03:
            raise ValueError('片段裁剪范围超过原视频时长')
        frames = round(duration * fps)
        if abs(duration - frames / fps) > .001:
            raise ValueError('片段时长必须对齐输出帧率')
        total_frames += frames
        if total_frames / fps > 600:
            raise ValueError('成片最长 10 分钟')
        prepared.append((source, start, frames, any(s['codec_type'] == 'audio' for s in info['streams'])))
    directory = store.directory / 'agent-media'
    directory.mkdir(exist_ok=True)
    ffmpeg = binary('ffmpeg')
    with tempfile.TemporaryDirectory(prefix='film-', dir=directory) as temporary:
        temporary = Path(temporary)
        for index, (source, start, frames, audio) in enumerate(prepared):
            length = frames / fps
            args = [ffmpeg, '-v', 'error', '-ss', start, '-i', source]
            if not audio:
                args += ['-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=stereo']
            video_filter = (f'scale={width}:{height}:force_original_aspect_ratio=decrease,'
                            f'pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},'
                            f'trim=end_frame={frames},setpts=N/({fps}*TB),format=yuv420p')
            await run_command([*args, '-map', '0:v:0', '-map', '0:a:0' if audio else '1:a:0',
                '-vf', video_filter, '-af', f'atrim=duration={length},asetpts=PTS-STARTPTS,apad',
                '-t', length, '-c:v', 'libx264', '-preset', 'fast', '-crf', '18',
                '-c:a', 'aac', '-ar', '48000', '-ac', '2', '-movflags', '+faststart', temporary / f'clip-{index:02d}.mp4'])
        manifest = temporary / 'clips.txt'
        manifest.write_text(''.join(f"file 'clip-{index:02d}.mp4'\n" for index in range(len(prepared))), encoding='utf-8')
        output = temporary / 'film.mp4'
        await run_command([ffmpeg, '-v', 'error', '-f', 'concat', '-safe', '1', '-i', manifest,
            '-map', '0:v:0', '-map', '0:a:0', '-vf', f'fps={fps},trim=end_frame={total_frames},setpts=N/({fps}*TB)',
            '-af', 'aresample=48000,asetpts=PTS-STARTPTS,apad', '-t', total_frames / fps,
            '-c:v', 'libx264', '-preset', 'fast', '-crf', '18', '-pix_fmt', 'yuv420p',
            '-c:a', 'aac', '-ar', '48000', '-ac', '2', '-movflags', '+faststart', output])
        final = await probe(output)
        video = next((stream for stream in final['streams'] if stream['codec_type'] == 'video'), {})
        if (video.get('codec_name') != 'h264' or (video.get('width'), video.get('height')) != (width, height)
                or Fraction(video.get('avg_frame_rate', '0')) != fps
                or int(video.get('nb_frames', 0)) != total_frames
                or abs(float(final['format']['duration']) - total_frames / fps) > .1
                or not any(stream['codec_type'] == 'audio' for stream in final['streams'])):
            raise RuntimeError('成片的帧数、时长、编码或音轨校验失败')
        return await register(store, output, name, {'agent_timeline': clips, 'fps': fps, 'frames': total_frames}, result_id)
