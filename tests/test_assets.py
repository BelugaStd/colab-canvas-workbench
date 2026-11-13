import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image

from backend.app import create_app
from backend.creative_runtime import register_result
from tests.fake_colab import PROFILES, FakeColab


@pytest.mark.parametrize('size', [(8000, 6000), (10000, 10000)])
async def test_large_images_upload_and_register_without_a_pixel_ceiling(tmp_path, size):
    source = tmp_path / 'large.png'
    with Image.new('1', size, 1) as image:
        image.save(source, 'PNG')
    content = source.read_bytes()
    fake = FakeColab()
    app = create_app(tmp_path, driver=fake, account_profiles=PROFILES)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
            client.headers['x-workbench-token'] = (await client.get('/api/state')).json()['token']
            response = await client.post('/api/assets', content=content)
            assert response.status_code == 201, response.text
            asset = response.json()
            assert (asset['width'], asset['height']) == size
            assert (await client.get(f"/api/assets/{asset['id']}/file")).content == content
            result = await register_result(app.state.store, source, 'test-job', 'upscale')
            assert (result['width'], result['height']) == size
            assert (await client.get(f"/api/assets/{result['id']}/file")).content == content
            assert not any(call[1] == 'create' for call in fake.calls)
