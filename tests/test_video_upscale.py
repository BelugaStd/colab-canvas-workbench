import shutil
import subprocess

import pytest

from backend.creative_workflows import compile_canvas
from backend.video_sr_remote import finish_video, output_dimensions, probe_video
from backend.workflows import validate_graph
from tests.test_creative_workflows import node

PRESETS = ('seedvr2-7b', 'seedvr2-7b-sharp', 'seedvr2-3b')


@pytest.mark.parametrize('model', PRESETS)
def test_uploaded_video_compiles_to_video_upscaler_and_video_output(model):
    canvas = {'nodes': [node('source', 'upload', asset_id='selected-version', media_type='video/mp4'),
                        node('sr', 'upscale', model=model, resolution='2×')],
              'edges': [{'source': 'source', 'target': 'sr'}]}
    compiled = compile_canvas(canvas, 'sr')
    validate_graph(compiled['graph'])
    graph = compiled['graph']
    assert compiled['asset_ids'] == ['selected-version']
    assert compiled['asset_media_types'] == {'selected-version': 'video/mp4'}
    assert compiled['models'] == [model]
    assert compiled['node_models']['sr'] == model
    assert any(n['class_type'] == 'LoadVideo' for n in graph.values())
    sr = next(n['inputs'] for n in graph.values() if n['class_type'] == 'WorkbenchVideoUpscale')
    assert sr['model'] == model
    assert sr['scale'] == 2
    assert graph[next(iter(compiled['output_nodes']))]['class_type'] == 'SaveVideo'
    assert not any(n['class_type'] == 'SaveImage' for n in graph.values())


def test_new_h3_to_video_upscale_preserves_export_settings_and_shared_dependency():
    canvas = {'nodes': [node('movie', 'video', duration=10, fps=30, mode='首尾循环', audio=True),
                        node('sr', 'upscale', resolution='4×')],
              'edges': [{'source': 'movie', 'target': 'sr'}]}
    compiled = compile_canvas(canvas, ['sr', 'movie'])
    assert compiled['asset_ids'] == []
    assert compiled['postprocess_nodes']['sr'] == {'fps': 30, 'duration': 10, 'loop': True}
    assert 'seedvr2-7b' in compiled['models']
    assert sum(n['class_type'] == 'MiniMaxH3ImageToVideo' for n in compiled['graph'].values()) == 1


def test_chained_video_upscale_stays_video_and_cannot_feed_image_generator():
    canvas = {'nodes': [node('source', 'upload', asset_id='chosen', media_type='video/webm'),
                        node('sr', 'upscale', resolution='2×'), node('second', 'upscale', model='seedvr2-3b', resolution='2×'),
                        node('image', 'image')],
              'edges': [{'source': 'source', 'target': 'sr'}, {'source': 'sr', 'target': 'second'},
                        {'source': 'second', 'target': 'image'}]}
    compiled = compile_canvas(canvas, 'second')
    assert sum(n['class_type'] == 'WorkbenchVideoUpscale' for n in compiled['graph'].values()) == 2
    with pytest.raises(ValueError, match='图片'):
        compile_canvas(canvas, 'image')


def test_invalid_upscale_model_is_rejected_before_allocation():
    canvas = {'nodes': [node('source', 'upload', asset_id='chosen', media_type='video/mp4'),
                        node('sr', 'upscale', model='unknown')],
              'edges': [{'source': 'source', 'target': 'sr'}]}
    with pytest.raises(ValueError, match='模型'):
        compile_canvas(canvas, 'sr')


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='ffmpeg required')
@pytest.mark.parametrize('audio,fps', [(True, '30000/1001'), (False, '24')])
def test_video_export_preserves_frames_fps_and_audio(tmp_path, audio, fps):
    source, enhanced, output = (tmp_path / name for name in ('source.mp4', 'enhanced.mp4', 'output.mp4'))
    args = ['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', f'testsrc2=size=64x48:rate={fps}']
    if audio:
        args += ['-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000']
    subprocess.run([*args, '-t', '1', '-c:v', 'libx264', '-c:a', 'aac', str(source)], check=True)
    subprocess.run(['ffmpeg', '-v', 'error', '-i', str(source), '-an', '-vf', 'scale=128:96', str(enhanced)], check=True)
    info = probe_video(source)
    finish_video(enhanced, source, output, info, (128, 96))
    result = probe_video(output)
    assert result['frames'] == info['frames']
    assert result['fps'] == info['fps']
    assert result['audio'] is audio
    assert (result['width'], result['height']) == (128, 96)
    truncated = tmp_path / 'truncated.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-i', str(enhanced), '-frames:v', '3', str(truncated)], check=True)
    with pytest.raises(RuntimeError, match='校验'):
        finish_video(truncated, source, output, info, (128, 96))


def test_output_dimensions_preserve_aspect_and_cap_long_edge_and_pixel_budget():
    assert output_dimensions(1344, 768, 2) == (2688, 1536)
    for w, h in [(1344, 768), (768, 1344), (1024, 1024), (3840, 2160)]:
        ow, oh = output_dimensions(w, h, 4)
        assert max(ow, oh) <= 3840 and ow * oh <= 3840 * 2160
        assert ow % 2 == oh % 2 == 0
        assert abs(ow / oh - w / h) < .003
    with pytest.raises(ValueError):
        output_dimensions(128, 128, 3)


@pytest.mark.parametrize('model', PRESETS)
@pytest.mark.skipif(not shutil.which('ffprobe'), reason='ffprobe required')
async def test_video_upload_to_completed_local_asset_and_version_settings(tmp_path, model):
    import asyncio

    from httpx import ASGITransport, AsyncClient

    from backend.app import create_app
    from backend.config import ROOT
    from tests.fake_colab import PROFILES, FakeColab

    fake = FakeColab(delay=.01)
    app = create_app(tmp_path, driver=fake, account_profiles=PROFILES)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
            client.headers['x-workbench-token'] = (await client.get('/api/state')).json()['token']
            uploaded = await client.post('/api/assets', content=(ROOT / 'tests/fixtures/synthetic.mp4').read_bytes())
            assert uploaded.status_code == 201, uploaded.text
            asset = uploaded.json()
            project = (await client.post('/api/projects', json={})).json()
            canvas = project['canvases'][0]
            canvas['nodes'] = [node('source', 'upload', asset_id=asset['id'], media_type='video/mp4'),
                               node('sr', 'upscale', model=model, resolution='2×')]
            canvas['edges'] = [{'id': 'e', 'source': 'source', 'target': 'sr'}]
            assert (await client.put('/api/projects/' + project['id'], json=project)).status_code == 200
            url = f"/api/projects/{project['id']}/canvases/{canvas['id']}/nodes/sr/generate"
            response = await client.post(url, json={'account': 'colab2'})
            assert response.status_code == 202, response.text
            job = response.json()
            for _ in range(120):
                result = (await client.get('/api/jobs/' + job['id'])).json()
                if result['status'] in {'completed', 'failed'}:
                    break
                await asyncio.sleep(.05)
            assert result['status'] == 'completed', result['message']
            assert len(result['outputs']) == 1
            output = next(o for o in result['outputs'] if o.get('creative_node') == 'sr')
            assert output['media_type'] == 'video/mp4'
            assert (await client.get('/api/assets/' + output['asset_id'] + '/file')).status_code == 200
            assert job['creative']['node_settings']['sr']['model'] == model
            assert fake.uploaded[0]['filename'] == f"creative-{asset['id']}.mp4"
