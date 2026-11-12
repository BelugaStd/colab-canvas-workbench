import asyncio
import io

from httpx import ASGITransport, AsyncClient
from PIL import Image

from backend.app import create_app
from tests.fake_colab import PROFILES, FakeColab


async def test_canvas_reference_generation_returns_assets_and_keeps_node_mapping(tmp_path):
    fake = FakeColab(delay=.01)
    app = create_app(tmp_path, driver=fake, account_profiles=PROFILES)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
            client.headers['x-workbench-token'] = (await client.get('/api/state')).json()['token']
            project = (await client.post('/api/projects', json={'name': 'Reference test'})).json()
            image = io.BytesIO()
            Image.new('RGB', (64, 64), 'red').save(image, 'JPEG')
            asset = (await client.post('/api/assets', content=image.getvalue())).json()
            canvas = project['canvases'][0]
            canvas['nodes'] = [
                {'id': 'ref', 'type': 'creative', 'position': {'x': 0, 'y': 0},
                 'data': {'kind': 'upload', 'label': '照片', 'asset_id': asset['id']}},
                {'id': 'image', 'type': 'creative', 'position': {'x': 400, 'y': 0},
                 'data': {'kind': 'image', 'label': '插画', 'prompt': '以@照片 为参考绘制森林'}}]
            canvas['edges'] = [{'id': 'e', 'source': 'ref', 'target': 'image'}]
            assert (await client.put('/api/projects/' + project['id'], json=project)).status_code == 200
            url = f"/api/projects/{project['id']}/canvases/{canvas['id']}/nodes/image/generate"
            response = await client.post(url, json={'account': 'colab2'})
            assert response.status_code == 202, response.text
            job = response.json()
            assert job['creative']['node_settings']['image']['prompt'] == '以@照片 为参考绘制森林'
            assert 'versions' not in job['creative']['node_settings']['image']
            duplicate = await client.post(url, json={'account': 'colab2'})
            assert duplicate.status_code == 409
            for _ in range(150):
                result = (await client.get('/api/jobs/' + job['id'])).json()
                if result['status'] not in {'pending', 'preparing', 'submitting', 'queued', 'running', 'downloading'}:
                    break
                await asyncio.sleep(.05)
            assert result['status'] == 'completed', result['message']
            output = result['outputs'][0]
            assert output['creative_node'] == 'image'
            stored_asset = app.state.store.get('assets', output['asset_id'])
            assert stored_asset['media_type'] == 'image/png'
            assert (await client.get('/api/assets/' + output['asset_id'] + '/file')).status_code == 200
            assert len(fake.prompt_posts) == 1
            assert fake.uploaded[0]['filename'] == f"creative-{asset['id']}.png"
            assert fake.uploaded[0]['content'].startswith(b'\x89PNG')
            # Results live on the run, so an independently edited canvas is not replaced.
            restored = (await client.get('/api/projects/' + project['id'])).json()
            assert restored['canvases'][0]['nodes'][1]['data']['prompt'] == '以@照片 为参考绘制森林'
            restored['canvases'][0]['nodes'][1]['data']['prompt'] = 'A blue mountain'
            assert (await client.put('/api/projects/' + project['id'], json=restored)).status_code == 200
            second = (await client.post(url, json={'account': 'colab2'})).json()
            assert second['creative']['node_settings']['image']['prompt'] == 'A blue mountain'
            first = (await client.get('/api/jobs/' + job['id'])).json()
            assert first['creative']['node_settings']['image']['prompt'] == '以@照片 为参考绘制森林'


async def test_invalid_reference_is_rejected_before_gpu_allocation(tmp_path):
    fake = FakeColab()
    app = create_app(tmp_path, driver=fake, account_profiles=PROFILES)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
            client.headers['x-workbench-token'] = (await client.get('/api/state')).json()['token']
            project = (await client.post('/api/projects', json={})).json()
            canvas = project['canvases'][0]
            canvas['nodes'] = [{'id': 'i', 'type': 'creative', 'position': {'x': 0, 'y': 0},
                                'data': {'kind': 'upscale', 'label': '超分'}}]
            await client.put('/api/projects/' + project['id'], json=project)
            response = await client.post(f"/api/projects/{project['id']}/canvases/{canvas['id']}/nodes/i/generate",
                                         json={'account': 'colab2'})
            assert response.status_code == 400
            assert not any(c[1] == 'create' for c in fake.calls)
