"""Resolve requested local asset IDs into bounded visual context at request time."""
import asyncio
import base64
import copy
import io
import json
from functools import lru_cache

from PIL import Image, ImageOps

from .assets import asset_path


@lru_cache(maxsize=32)
def thumbnail(path, modified, size):
    with Image.open(path) as source:
        source.draft('RGB', (1024, 1024))
        image = ImageOps.exif_transpose(source)
        image.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
        image = image.convert('RGB')
        stream = io.BytesIO()
        image.save(stream, 'JPEG', quality=85)
    return 'data:image/jpeg;base64,' + base64.b64encode(stream.getvalue()).decode('ascii')


async def visual_history(store, history):
    result = copy.deepcopy(history)
    for message in result:
        if message.get('role') != 'tool':
            continue
        try:
            descriptors = json.loads(message.get('content', '{}')).get('_visual_assets', [])
        except (ValueError, AttributeError):
            continue
        if not isinstance(descriptors, list) or len(descriptors) > 4:
            raise ValueError('一次视觉检查最多四张图片')
        images = []
        for descriptor in descriptors:
            key = descriptor['asset_id']
            path, asset = asset_path(store, key)
            if not asset['media_type'].startswith('image/'):
                raise ValueError('视频请先提取帧，再进行视觉检查')
            stat = path.stat()
            url = await asyncio.to_thread(thumbnail, str(path), stat.st_mtime_ns, stat.st_size)
            images.append({'asset_id': key, 'data_url': url})
        if images:
            message['images'] = images
    return result
