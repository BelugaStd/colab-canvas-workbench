import shutil
import subprocess

import pytest
from PIL import Image, ImageChops, ImageStat

from backend.assets import inspect_media
from backend.creative_runtime import command, export_video


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='ffmpeg required')
@pytest.mark.parametrize('fps,loop', [(24, True), (30, True), (30, False)])
async def test_video_export_has_exact_duration_fps_and_continuous_motion(tmp_path, fps, loop):
    ffmpeg = shutil.which('ffmpeg')
    source = tmp_path / 'native.mp4'
    await command(ffmpeg, '-y', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=128x128:rate=24',
                  '-t', '5.2', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(source))
    result = await export_video(source, {'duration': 5, 'fps': fps, 'loop': loop})
    info = await inspect_media(result)
    assert abs(info['duration'] - 5) < .05
    raw = subprocess.check_output([ffmpeg, '-v', 'error', '-i', str(result), '-vf', 'scale=64:64',
                                   '-pix_fmt', 'gray', '-f', 'rawvideo', '-'])
    frames = [raw[i:i + 4096] for i in range(0, len(raw), 4096)]
    assert len(frames) == 5 * fps
    for second in range(5):
        sample = frames[second * fps:(second + 1) * fps]
        differences = [sum(abs(a-b) for a, b in zip(x, y)) / 4096 for x, y in zip(sample, sample[1:])]
        assert sum(differences) / len(differences) > .5, f'motion froze during second {second}'
    await command(ffmpeg, '-y', '-v', 'error', '-i', str(result), '-vf', rf'select=eq(n\,0)+eq(n\,{5*fps-1})',
                  '-vsync', '0', str(tmp_path / '%d.png'))
    with Image.open(tmp_path / '1.png') as first, Image.open(tmp_path / '2.png') as last:
        if loop:
            assert max(ImageStat.Stat(ImageChops.difference(first, last)).mean) < 4
    assert source.is_file()
