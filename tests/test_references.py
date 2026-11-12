import pytest

from backend.creative_workflows import compile_canvas
from tests.test_creative_workflows import node


def inputs(result, kind):
    return next(n['inputs'] for n in result['graph'].values() if n['class_type'] == kind)


@pytest.mark.parametrize('mode,count', [('首帧', 0), ('首尾帧', 0), ('首尾帧', 1), ('首尾循环', 0), ('文生视频', 1)])
def test_explicit_video_mode_must_match_its_inputs(mode, count):
    canvas = {'nodes': [node('v', 'video', mode=mode)] + [node(f'r{i}', 'upload', asset_id=f'a{i}') for i in range(count)],
              'edges': [{'source': f'r{i}', 'target': 'v'} for i in range(count)]}
    with pytest.raises(ValueError):
        compile_canvas(canvas, 'v')


def test_explicit_frame_roles_and_stable_aliases_survive_edge_reordering():
    canvas = {'nodes': [node('a', 'upload', asset_id='first', label='same'), node('b', 'upload', asset_id='last', label='same'),
                        node('v', 'video', mode='首尾帧', prompt='从@图2 过渡到@图1')],
              'edges': [{'source': 'b', 'target': 'v', 'data': {'alias': '图1', 'role': 'last'}},
                        {'source': 'a', 'target': 'v', 'data': {'alias': '图2', 'role': 'first'}}]}
    result = compile_canvas(canvas, 'v')
    video = inputs(result, 'MiniMaxH3ImageToVideo')
    assert video['prompt'] == '从<Picture 1> 过渡到<Picture 2>'
    assert result['reference_plans']['v']['images'] == ['a', 'b']
    graph = result['graph']
    assert graph[graph[video['first_frame'][0]]['inputs']['image'][0]]['inputs']['image'] == 'creative-first.png'
    canvas['edges'].reverse()
    again = compile_canvas(canvas, 'v')
    assert inputs(again, 'MiniMaxH3ImageToVideo')['prompt'] == video['prompt']


def test_duplicate_legacy_names_and_unconnected_mentions_fail_before_allocation():
    for prompt in ['Edit @same', 'Edit @missing']:
        canvas = {'nodes': [node('a', 'upload', asset_id='a', label='same'), node('b', 'upload', asset_id='b', label='same'),
                            node('i', 'image', prompt=prompt)],
                  'edges': [{'source': 'a', 'target': 'i'}, {'source': 'b', 'target': 'i'}]}
        with pytest.raises(ValueError, match='引用'):
            compile_canvas(canvas, 'i')


def test_primary_image_role_controls_edit_subject_and_picture_number():
    canvas = {'nodes': [node('a', 'upload', asset_id='a'), node('b', 'upload', asset_id='b'),
                        node('i', 'image', prompt='Edit @图2 using @图1', model='minimax-image')],
              'edges': [{'source': 'a', 'target': 'i', 'data': {'alias': '图1', 'role': 'reference'}},
                        {'source': 'b', 'target': 'i', 'data': {'alias': '图2', 'role': 'base'}}]}
    result = compile_canvas(canvas, 'i')
    positive = inputs(result, 'MiniMaxH3ImageConditioning')
    assert positive['prompt'] == 'Edit <Picture 1> using <Picture 2>'
    assert result['graph'][positive['images.image_0'][0]]['inputs']['image'] == 'creative-b.png'


def test_reference_video_uses_ref2va_weights_and_all_images():
    canvas = {'nodes': [node('a', 'upload', asset_id='a'), node('b', 'upload', asset_id='b'),
                        node('v', 'video', mode='参考图', prompt='@图1 and @图2 walking together')],
              'edges': [{'source': 'a', 'target': 'v', 'data': {'alias': '图1'}},
                        {'source': 'b', 'target': 'v', 'data': {'alias': '图2'}}]}
    result = compile_canvas(canvas, 'v')
    refs = inputs(result, 'MiniMaxH3ReferenceToVideo')
    assert 'ref_images.ref_image_0' in refs and 'ref_images.ref_image_1' in refs
    assert 'minimax-h3-reference' in result['models']
    assert inputs(result, 'UNETLoader')['unet_name'] == 'minimax_h3_ref2va_pruned_int8_convrot.safetensors'


def test_connected_text_is_used_without_requiring_a_typed_mention():
    canvas = {'nodes': [node('script', 'text', prompt='A tiny red robot on a blue table'), node('i', 'image', prompt='')],
              'edges': [{'source': 'script', 'target': 'i'}]}
    assert 'tiny red robot' in inputs(compile_canvas(canvas, 'i'), 'MiniMaxH3ImageConditioning')['prompt']


def test_edit_matches_source_aspect_and_official_moss_guidance():
    canvas = {'nodes': [node('a', 'upload', asset_id='a'), node('i', 'image', model='moss-image', aspect='source', resolution='2MP')],
              'edges': [{'source': 'a', 'target': 'i'}]}
    result = compile_canvas(canvas, 'i', asset_info={'a': {'width': 640, 'height': 480}})
    latent = inputs(result, 'EmptyFlux2LatentImage')
    assert abs(latent['width'] / latent['height'] - 4 / 3) < .02
    assert latent['width'] * latent['height'] < 2_100_000
    guidance = inputs(result, 'SkipLayerGuidanceDiTSimple')
    assert guidance['single_layers'] == '22'
    assert guidance['double_layers'] == ''


def test_h3_768p_uses_official_canvas_without_rounding_height_down_to_704():
    result=compile_canvas({'nodes':[node('v','video',aspect='16:9',resolution='768p')],'edges':[]},'v')
    assert result['node_dimensions']['v']==(1344,768)
