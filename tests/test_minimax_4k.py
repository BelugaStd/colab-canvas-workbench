import pytest

from backend.creative_workflows import compile_canvas
from backend.workflows import validate_graph
from tests.test_creative_workflows import node


@pytest.mark.parametrize('reference', [False, True])
@pytest.mark.parametrize('aspect,size', [('1:1', (4096, 4096)), ('16:9', (5472, 3072))])
def test_minimax_4k_samples_at_native_size_without_automatic_upscaling(reference, aspect, size):
    nodes = [node('image', 'image', model='minimax-image', resolution='4K', aspect=aspect,
                  quality='标准', image_mode='edit' if reference else 'text')]
    edges = []
    if reference:
        nodes.append(node('source', 'upload', asset_id='reference', width=800, height=600))
        edges.append({'source': 'source', 'target': 'image', 'data': {'alias': '图1', 'role': 'base'}})
    result = compile_canvas({'nodes': nodes, 'edges': edges}, 'image')
    validate_graph(result['graph'])
    conditioning = next(n for n in result['graph'].values() if n['class_type'] == 'MiniMaxH3ImageConditioning')
    assert (conditioning['inputs']['width'], conditioning['inputs']['height']) == size
    sampler = next(n for n in result['graph'].values() if n['class_type'] == 'KSampler')
    assert sampler['inputs']['latent_image'] == [next(k for k, n in result['graph'].items() if n is conditioning), 1]
    assert result['node_dimensions']['image'] == size
    assert result['models'] == ['minimax-image-full']
    assert not any(n['class_type'] == 'ImageUpscaleWithModel' for n in result['graph'].values())
    assert ('images.image_0' in conditioning['inputs']) is reference


@pytest.mark.parametrize('model', ['moss-image', 'qwen-edit'])
def test_native_4k_is_not_enabled_for_other_image_models(model):
    canvas = {'nodes': [node('source', 'upload', asset_id='reference'),
                        node('image', 'image', model=model, resolution='4K', image_mode='edit')],
              'edges': [{'source': 'source', 'target': 'image'}]}
    with pytest.raises(ValueError, match='分辨率'):
        compile_canvas(canvas, 'image')
