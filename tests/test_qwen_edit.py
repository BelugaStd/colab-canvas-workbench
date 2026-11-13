import pytest

from backend.config import CREATIVE_MODELS
from backend.creative_workflows import compile_canvas
from backend.deploy_remote import MODEL_GROUPS


def canvas(count=1, **settings):
    nodes = [{'id': str(i), 'data': {'kind': 'upload', 'label': f'input{i}', 'asset_id': f'asset{i}', 'width': 800, 'height': 600}} for i in range(count)]
    nodes.append({'id': 'edit', 'data': {'kind': 'image', 'label': 'edit', 'model': 'qwen-edit', 'image_mode': 'edit',
                                       'prompt': '把@图1 的杯子改成蓝色', 'aspect': 'source', 'resolution': '1MP', **settings}})
    return {'nodes': nodes, 'edges': [{'source': str(i), 'target': 'edit', 'data': {'alias': f'图{i+1}', 'role': 'base' if i == 0 else 'reference'}} for i in range(count)]}


@pytest.mark.parametrize('count', [1, 2, 3])
def test_qwen_edit_uses_native_plus_conditioning_and_exact_reference_order(count):
    result = compile_canvas(canvas(count), 'edit')
    assert result['models'] == ['qwen-edit']
    graph = result['graph']
    encoders = [n['inputs'] for n in graph.values() if n['class_type'] == 'TextEncodeQwenImageEditPlus']
    assert len(encoders) == 2
    assert all(all(f'image{i+1}' in n for i in range(count)) for n in encoders)
    assert 'Picture 1' in encoders[0]['prompt'] and '<Picture' not in encoders[0]['prompt']
    assert any(n['class_type'] == 'VAEEncode' for n in graph.values())
    sampler = next(n['inputs'] for n in graph.values() if n['class_type'] == 'KSampler')
    assert (sampler['steps'], sampler['cfg'], sampler['sampler_name']) == (40, 4.0, 'euler')
    assert result['node_dimensions']['edit'][0] / result['node_dimensions']['edit'][1] == pytest.approx(4/3, rel=.025)


@pytest.mark.parametrize('count', [0, 4])
def test_qwen_edit_rejects_missing_or_extra_images(count):
    with pytest.raises(ValueError, match='Qwen.*1.*3|千问.*1.*3'):
        compile_canvas(canvas(count, image_mode='text' if count == 0 else 'edit', prompt='test'), 'edit')


def test_qwen_deployment_pins_official_files_and_exposes_new_preset():
    assert 'qwen-edit' in CREATIVE_MODELS
    files = MODEL_GROUPS['qwen-edit']
    assert len(files) == 3
    assert all(len(revision) == 40 for _, revision, _ in files)
    assert any('2511_int8_convrot' in filename for _, _, filename in files)
    assert all(repository.startswith('Comfy-Org/') for repository, _, _ in files)
