import asyncio
import json
import shutil
import subprocess
import threading
import uuid

import pytest
from fastapi import HTTPException
from PIL import Image

from backend.agent_media import assemble, extract, register, run_command
from backend.assets import inspect_media
from backend.store import Store


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path)
    yield value
    value.close()


async def add_video(store, *, audio=False, color='red', fps=24):
    key = uuid.uuid4().hex
    directory = store.directory / 'assets'
    directory.mkdir(exist_ok=True)
    path = directory / (key + '.mp4')
    command = ['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', f'color=c={color}:s=64x48:r={fps}']
    if audio:
        command += ['-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000']
    subprocess.run([*command, '-t', '1', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(path)], check=True)
    info = await inspect_media(path)
    store.put('assets', key, {'id': key, 'name': 'test', 'created_at': 1, **info})
    return key


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='ffmpeg required')
async def test_assembly_normalizes_mixed_audio_fps_and_verifies_exact_duration(store):
    first, second = await add_video(store), await add_video(store, audio=True, color='blue', fps=30)
    result = await assemble(store, [{'asset_id': first, 'start': 0, 'duration': 1},
                                   {'asset_id': second, 'start': 0, 'duration': 1}], 'Film', 24, 128, 96)
    path = store.directory / 'assets' / (result['id'] + '.mp4')
    probe = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(path)]))
    video = next(s for s in probe['streams'] if s['codec_type'] == 'video')
    assert video['codec_name'] == 'h264'
    assert video['width'] == 128 and video['height'] == 96
    assert video['avg_frame_rate'] == '24/1' and int(video['nb_frames']) == 48
    assert abs(float(probe['format']['duration']) - 2) < .1
    assert any(s['codec_type'] == 'audio' for s in probe['streams'])
    assert (store.directory / 'assets' / (first + '.mp4')).exists()


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='ffmpeg required')
async def test_frame_extraction_registers_actual_frame_and_rejects_outside_duration(store):
    source = await add_video(store, color='red')
    result = await extract(store, source, .5, 'Middle frame')
    with Image.open(store.directory / 'assets' / (result['id'] + '.png')) as image:
        r, g, b = image.convert('RGB').getpixel((20, 20))
        assert r > 200 and g < 25 and b < 25
    with pytest.raises(ValueError):
        await extract(store, source, 3, 'Outside')


@pytest.mark.parametrize('clips,fps,width,height', [([], 24, 128, 96),
    ([{'asset_id': '../../file', 'start': 0, 'duration': 1}], 24, 128, 96),
    ([{'asset_id': 'a' * 32, 'start': 0, 'duration': 1}], 240, 128, 96)])
async def test_invalid_assembly_never_creates_output(store, clips, fps, width, height):
    with pytest.raises((ValueError, HTTPException)):
        await assemble(store, clips, 'Invalid', fps, width, height)
    assert store.all('assets') == []


async def test_command_cancellation_kills_child(monkeypatch):
    killed, waited = [], []
    class Process:
        returncode = None
        async def communicate(self):
            await asyncio.Event().wait()
        def kill(self):
            killed.append(True)
            self.returncode = -9
        async def wait(self):
            waited.append(True)
            return -9
    async def launch(*args, **kwargs):
        return Process()
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', launch)
    task = asyncio.create_task(run_command(['ffmpeg', '-version']))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert killed and waited


async def test_cancelled_registration_waits_for_copy_before_cleanup(store, monkeypatch, tmp_path):
    source = tmp_path / 'source.png'
    Image.new('RGB', (32, 32)).save(source)
    entered, release, copied = threading.Event(), threading.Event(), threading.Event()
    original = shutil.copyfile
    def delayed_copy(*args):
        entered.set()
        assert release.wait(5)
        result = original(*args)
        copied.set()
        return result
    monkeypatch.setattr(shutil, 'copyfile', delayed_copy)
    task = asyncio.create_task(register(store, source, 'cancelled'))
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.to_thread(copied.wait, 5)
    assert store.all('assets') == []
    assert list((store.directory / 'assets').iterdir()) == []
