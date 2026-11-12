"""Regressions for transient runtime and local result recovery failures."""
import asyncio
import time

import pytest
from aiohttp import web

from backend.creative_workflows import compile_canvas
from backend.manager import Manager
from backend.store import Store
from tests.fake_colab import PROFILES, FakeColab, template
from tests.test_creative_workflows import node


async def eventually(condition, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        await asyncio.sleep(.03)
    raise AssertionError('Condition did not become true')


async def test_transient_history_http_503_recovers_original_prompt_without_new_gpu(tmp_path, monkeypatch):
    fake, store = FakeColab(), Store(tmp_path)
    manager = Manager(store, fake, PROFILES)
    await manager.open()
    temporary = web.Application()
    async def unavailable(_):
        return web.Response(status=503, text='Runtime temporarily busy')
    temporary.router.add_get('/temporary-history', unavailable)
    runner = web.AppRunner(temporary)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    temporary_port = site._server.sockets[0].getsockname()[1]
    original_request = manager.http.request
    failures = []

    def request(method, url, **kwargs):
        if '/history/' in str(url) and not failures:
            failures.append(url)
            url = f'http://127.0.0.1:{temporary_port}/temporary-history'
        return original_request(method, url, **kwargs)

    monkeypatch.setattr(manager.http, 'request', request)
    try:
        job = manager.submit('colab2', template(), 'Transient history', False)
        await eventually(lambda: manager.job(job['id'])['status'] in {'completed', 'failed'})
        result = manager.job(job['id'])
        assert result['status'] == 'completed', result['message']
        assert len(fake.prompt_posts) == 1
        assert len([call for call in fake.calls if call[1] == 'create']) == 1
        assert failures
        assert result['outputs']
    finally:
        await manager.close()
        await runner.cleanup()
        store.close()


def test_minimax_image_aspects_follow_official_32_pixel_grid():
    for aspect in ('16:9', '3:2', '4:3', '1:1', '3:4', '9:16'):
        canvas = {'nodes': [node('image', 'image', model='minimax-image', resolution='2K', aspect=aspect)], 'edges': []}
        result = compile_canvas(canvas, 'image')
        width, height = result['node_dimensions']['image']
        assert width % 32 == height % 32 == 0, (aspect, width, height)


def test_video_upscale_dimension_metadata_matches_runtime_4k_cap():
    canvas = {'nodes': [node('video', 'upload', asset_id='v', media_type='video/mp4'),
                        node('upscale', 'upscale', model='seedvr2-7b', resolution='4×')],
              'edges': [{'source': 'video', 'target': 'upscale'}]}
    result = compile_canvas(canvas, 'upscale', asset_info={'v': {'width': 1920, 'height': 1080}})
    assert result['node_dimensions']['upscale'] == (3840, 2160)


def test_image_mentions_in_connected_text_are_resolved_in_destination_context():
    canvas = {'nodes': [node('photo', 'upload', asset_id='ref'),
                        node('script', 'text', prompt='Edit @图1 so the cup is blue.'),
                        node('image', 'image', prompt='')],
              'edges': [{'source': 'photo', 'target': 'image', 'data': {'alias': '图1', 'role': 'base'}},
                        {'source': 'script', 'target': 'image', 'data': {'alias': '文本2'}}]}
    result = compile_canvas(canvas, 'image')
    positive = next(n['inputs'] for n in result['graph'].values() if n['class_type'] == 'MiniMaxH3ImageConditioning')
    assert positive['prompt'] == 'Edit <Picture 1> so the cup is blue.'


def test_unconnected_legacy_name_cannot_match_a_connected_names_prefix():
    canvas = {'nodes': [node('photo', 'upload', asset_id='ref', label='cat'),
                        node('image', 'image', prompt='Use @catalog.png as the reference.')],
              'edges': [{'source': 'photo', 'target': 'image', 'data': {'alias': '图1'}}]}
    with pytest.raises(ValueError, match='引用'):
        compile_canvas(canvas, 'image')


async def test_deployment_status_timeout_keeps_same_machine_and_waits_for_readiness(tmp_path,monkeypatch):
    fake,store=FakeColab(),Store(tmp_path)
    manager=Manager(store,fake,PROFILES)
    await manager.open()
    original_remote=manager.remote
    checks=[]
    async def remote(account,method,path,*args,**kwargs):
        if path=='/system_stats' and not checks:
            raise ConnectionError('service is still installing')
        return await original_remote(account,method,path,*args,**kwargs)
    original_execute=fake.execute
    async def execute(account,session,script):
        if script.name.endswith('-check.py') and not checks:
            checks.append(session)
            raise TimeoutError()
        return await original_execute(account,session,script)
    monkeypatch.setattr(manager,'remote',remote)
    monkeypatch.setattr(fake,'execute',execute)
    try:
        await manager.start('colab2')
        assert manager.machine('colab2')['phase']=='ready',manager.machine('colab2')['message']
        assert len([c for c in fake.calls if c[1]=='create'])==1
        assert checks
    finally:
        await manager.close()
        store.close()


async def test_readonly_session_query_retries_transient_tls_failure(tmp_path,monkeypatch):
    from backend.colab import Colab
    colab=Colab(PROFILES,tmp_path)
    calls=[]
    async def command(account,*args,**kwargs):
        calls.append(args)
        if len(calls)==1:
            raise RuntimeError('SSLError: UNEXPECTED_EOF_WHILE_READING')
        return '[colab] No active sessions found on server.'
    monkeypatch.setattr(colab,'command',command)
    assert await colab.sessions('colab2')==[]
    assert calls==[('sessions',),('sessions',)]


def test_reference_dimensions_follow_exif_orientation(tmp_path):
    from PIL import Image

    from backend.assets import image_info
    from backend.creative_runtime import reference_png
    path=tmp_path/'rotated.jpg'
    exif=Image.Exif()
    exif[274]=6
    Image.new('RGB',(640,480),'red').save(path,exif=exif)
    info=image_info(path)
    import io
    actual=Image.open(io.BytesIO(reference_png(path))).size
    assert (info['width'],info['height'])==actual==(480,640)
