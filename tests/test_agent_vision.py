import json

import pytest
from PIL import Image

from backend.agent_vision import visual_history
from backend.store import Store


async def test_vision_uses_validated_local_images_without_persisting_data_urls(tmp_path):
    store = Store(tmp_path)
    try:
        directory = tmp_path / 'assets'
        directory.mkdir()
        key = 'a' * 32
        Image.new('RGB', (2000, 1500), 'red').save(directory / (key + '.png'))
        store.put('assets', key, {'id': key, 'extension': '.png', 'media_type': 'image/png'})
        history = [{'role': 'tool', 'tool_call_id': 'call', 'content': json.dumps({'_visual_assets': [{'asset_id': key}]})}]
        hydrated = await visual_history(store, history)
        assert hydrated[0]['images'][0]['data_url'].startswith('data:image/jpeg;base64,')
        assert 'images' not in history[0]
        assert 'base64' not in json.dumps(store.all('assets'))
        with pytest.raises(Exception):
            await visual_history(store, [{'role': 'tool', 'content': json.dumps({'_visual_assets': [{'asset_id': '../secret'}]})}])
    finally:
        store.close()
