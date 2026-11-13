import copy
import uuid

import pytest
from fastapi import HTTPException
from PIL import Image

from backend.agent_tools import TOOLS, ToolApprovalRequired, ToolContext, execute_tool
from backend.manager import Manager
from backend.store import Store
from tests.fake_colab import PROFILES, FakeColab
from tests.test_creative_workflows import node


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    store = Store(tmp_path)
    manager = Manager(store, FakeColab(), PROFILES)
    monkeypatch.setattr(manager, 'ensure_worker', lambda account: None)
    store.put('projects', 'project', {'id': 'project', 'kind': 'project', 'name': 'Film', 'revision': 0,
        'created_at': 1, 'updated_at': 1, 'deleted_at': None,
        'canvases': [{'id': 'canvas', 'name': 'Film', 'nodes': [], 'edges': [],
                      'viewport': {'x': 0, 'y': 0, 'zoom': 1}}]})
    yield ToolContext(manager, 'project', 'canvas', 'colab2', 'run', 'call')
    store.close()


def asset(ctx, color='blue'):
    key = uuid.uuid4().hex
    directory = ctx.manager.store.directory / 'assets'
    directory.mkdir(exist_ok=True)
    Image.new('RGB', (32, 32), color).save(directory / (key + '.png'))
    ctx.manager.store.put('assets', key, {'id': key, 'name': color, 'extension': '.png',
        'media_type': 'image/png', 'created_at': 1, 'width': 32, 'height': 32})
    return key


async def edit(ctx, operations, revision=0):
    return await execute_tool('edit_canvas', {'expected_revision': revision, 'operations': operations}, ctx)


def create(key, kind='image', **data):
    return {'op': 'create', 'node': node(key, kind, **data)}


def test_tools_have_stable_unique_names_and_closed_schemas():
    assert [tool['name'] for tool in TOOLS] == ['inspect_canvas', 'inspect_assets', 'edit_canvas',
        'save_story', 'generate_nodes', 'get_jobs', 'cancel_job', 'resume_job', 'server',
        'extract_frame', 'assemble_film', 'manage_project']
    assert all(tool['parameters']['additionalProperties'] is False for tool in TOOLS)


async def test_batch_graph_creation_is_atomic_and_rejects_stale_revision(ctx):
    before = ctx.manager.store.get('projects', 'project')
    with pytest.raises(ValueError):
        await edit(ctx, [create('a'), {'op': 'connect', 'source': 'a', 'target': 'missing'}])
    assert ctx.manager.store.get('projects', 'project') == before
    result = await edit(ctx, [create('a')])
    assert result['revision'] == 1
    with pytest.raises(HTTPException) as error:
        await edit(ctx, [{'op': 'update', 'node_id': 'a', 'data': {'prompt': 'overwrite'}}])
    assert error.value.status_code == 409
    assert ctx.manager.store.get('projects', 'project')['canvases'][0]['nodes'][0]['data']['prompt'] == 'A forest'


async def test_first_last_reference_roles_and_all_models_are_validated(ctx):
    operations = [create('first'), create('last'), create('video', 'video', mode='首尾帧'),
        {'op': 'connect', 'source': 'first', 'target': 'video', 'role': 'first', 'alias': '图1'},
        {'op': 'connect', 'source': 'last', 'target': 'video', 'role': 'last', 'alias': '图2'}]
    result = await edit(ctx, operations)
    assert result['revision'] == 1
    before = ctx.manager.store.get('projects', 'project')
    with pytest.raises(ValueError):
        await edit(ctx, [{'op': 'update', 'node_id': 'video', 'data': {'resolution': '4K'}}], 1)
    assert ctx.manager.store.get('projects', 'project') == before
    with pytest.raises(ValueError):
        await edit(ctx, [{'op': 'connect', 'source': 'video', 'target': 'first'}], 1)
    assert ctx.manager.store.get('projects', 'project') == before


async def test_invalid_properties_and_foreign_job_are_rejected(ctx):
    with pytest.raises(ValueError):
        await execute_tool('inspect_canvas', {'shell': 'anything'}, ctx)
    ctx.manager.store.put('jobs', 'foreign', {'id': 'foreign', 'account': 'colab1', 'status': 'running',
        'creative': {'project_id': 'elsewhere', 'canvas_id': 'canvas'}})
    with pytest.raises((HTTPException, ValueError)):
        await execute_tool('get_jobs', {'job_ids': ['foreign']}, ctx)
    with pytest.raises(ValueError):
        await edit(ctx, [create('image', model='not-a-model')])


async def test_plan_mode_edits_but_never_allocates_or_submits(ctx):
    ctx.mode = 'plan'
    await edit(ctx, [create('a')])
    with pytest.raises(ValueError, match='规划|plan|生成'):
        await execute_tool('generate_nodes', {'node_ids': ['a'], 'expected_revision': 1}, ctx)
    with pytest.raises(ValueError):
        await execute_tool('server', {'action': 'connect'}, ctx)
    assert ctx.manager.store.all('jobs') == []
    assert not ctx.manager.colab.calls


async def test_generation_reuses_outputs_and_regenerate_creates_new_job(ctx):
    key = asset(ctx)
    await edit(ctx, [create('a', asset_id=key, media_type='image/png')])
    reused = await execute_tool('generate_nodes', {'node_ids': ['a'], 'expected_revision': 1, 'regenerate': False}, ctx)
    assert reused['job_id'] is None and reused['reused_nodes'] == ['a']
    generated = await execute_tool('generate_nodes', {'node_ids': ['a'], 'expected_revision': 1, 'regenerate': True}, ctx)
    assert generated['job_id']
    assert len(ctx.manager.store.all('jobs')) == 1


async def test_completed_job_merge_populates_dependency_without_overriding_selected_version(ctx):
    old, newest = asset(ctx), asset(ctx, 'red')
    await edit(ctx, [create('a', asset_id=old, media_type='image/png', active_version_id=old,
        versions=[{'id': old, 'asset_id': old, 'media_type': 'image/png', 'created_at': 0}],
        version_selection='manual')])
    ctx.manager.store.put('jobs', 'done', {'id': 'done', 'account': 'colab2', 'status': 'completed',
        'created_at': 2, 'outputs': [{'creative_node': 'a', 'asset_id': newest, 'media_type': 'image/png'}],
        'creative': {'project_id': 'project', 'canvas_id': 'canvas', 'node_settings': {'a': {'prompt': 'red'}}}})
    result = await execute_tool('get_jobs', {'job_ids': ['done']}, ctx)
    assert result['revision'] == 2
    data = ctx.manager.store.get('projects', 'project')['canvases'][0]['nodes'][0]['data']
    assert data['asset_id'] == old
    assert {v['asset_id'] for v in data['versions']} == {old, newest}
    again = await execute_tool('get_jobs', {'job_ids': ['done']}, ctx)
    assert again['revision'] == 2
    selected = await edit(ctx, [{'op': 'select_version', 'node_id': 'a', 'version_id': newest}], 2)
    assert selected['revision'] == 3


async def test_generate_downstream_merges_upstream_completed_output(ctx):
    key = asset(ctx)
    await edit(ctx, [create('a'), create('video', 'video', mode='首帧'),
                    {'op': 'connect', 'source': 'a', 'target': 'video', 'role': 'first'}])
    ctx.manager.store.put('jobs', 'done', {'id': 'done', 'account': 'colab2', 'status': 'completed',
        'created_at': 2, 'outputs': [{'creative_node': 'a', 'asset_id': key, 'media_type': 'image/png'}],
        'creative': {'project_id': 'project', 'canvas_id': 'canvas', 'node_settings': {'a': {'prompt': 'blue'}}}})
    result = await execute_tool('generate_nodes', {'node_ids': ['video'], 'expected_revision': 1}, ctx)
    job = ctx.manager.job(result['job_id'])
    assert job['creative']['asset_ids'] == [key]
    data = ctx.manager.store.get('projects', 'project')['canvases'][0]['nodes'][0]['data']
    assert data['version_selection'] == 'auto'
    assert not any(value['class_type'] == 'WorkbenchMiniMaxImageFullLoader' for value in job['graph'].values())


async def test_server_stop_requires_visible_approval_even_in_auto(ctx):
    with pytest.raises(ToolApprovalRequired):
        await execute_tool('server', {'action': 'stop'}, ctx)
    assert not ctx.manager.colab.calls


async def test_save_story_is_revision_guarded_and_stored_on_canvas(ctx):
    result = await execute_tool('save_story', {'expected_revision': 0, 'brief': {'title': 'The last light'},
        'characters': [{'name': 'Mira', 'identity': 'short hair, yellow coat'}],
        'shots': [{'id': 'shot1', 'duration': 10, 'beat': 'A choice'}]}, ctx)
    assert result['revision'] == 1
    canvas = ctx.manager.store.get('projects', 'project')['canvases'][0]
    assert canvas['agent_story']['shots'][0]['id'] == 'shot1'
    before = copy.deepcopy(canvas)
    with pytest.raises(ValueError):
        await execute_tool('save_story', {'expected_revision': 1, 'shots': [{'duration': -5}]}, ctx)
    assert ctx.manager.store.get('projects', 'project')['canvases'][0] == before


async def test_generation_idempotence_precedes_stale_revision_and_is_persisted_before_worker(ctx, monkeypatch):
    await edit(ctx, [create('a')])
    observed = []
    monkeypatch.setattr(ctx.manager, 'ensure_worker', lambda account: observed.extend(ctx.manager.store.all('jobs')))
    args = {'node_ids': ['a'], 'expected_revision': 1}
    first = await execute_tool('generate_nodes', args, ctx)
    assert observed[0]['creative']['agent_run_id'] == ctx.run_id
    assert observed[0]['creative']['agent_call_id'] == ctx.call_id
    await edit(ctx, [{'op': 'update', 'node_id': 'a', 'data': {'prompt': 'Changed while waiting'}}], 1)
    second = await execute_tool('generate_nodes', args, ctx)
    assert first['job_ids'] == second['job_ids']
    assert second['reconciled'] and second['revision'] == 2
    assert len(ctx.manager.store.all('jobs')) == 1


async def test_explicit_regeneration_activates_new_target_output_and_keeps_dependency_selection(ctx):
    anchor, previous, newest = asset(ctx), asset(ctx, 'red'), asset(ctx, 'green')
    await edit(ctx, [create('anchor', asset_id=anchor, media_type='image/png', version_selection='manual'),
        create('scene', asset_id=previous, media_type='image/png', version_selection='manual', image_mode='reference'),
        {'op': 'connect', 'source': 'anchor', 'target': 'scene'}])
    result = await execute_tool('generate_nodes', {'node_ids': ['scene'], 'expected_revision': 1, 'regenerate': True}, ctx)
    nodes = {node['id']: node['data'] for node in ctx.manager.store.get('projects', 'project')['canvases'][0]['nodes']}
    assert nodes['anchor']['version_selection'] == 'manual'
    assert nodes['scene']['version_selection'] == 'auto'
    assert nodes['scene']['asset_id'] == previous
    assert result['revision'] == 2
    ctx.manager.job(result['job_id'], status='completed', outputs=[{'creative_node': 'scene', 'asset_id': newest, 'media_type': 'image/png'}])
    await execute_tool('get_jobs', {'job_ids': result['job_ids'], 'wait': False}, ctx)
    nodes = {node['id']: node['data'] for node in ctx.manager.store.get('projects', 'project')['canvases'][0]['nodes']}
    assert nodes['anchor']['asset_id'] == anchor
    assert nodes['scene']['asset_id'] == newest
    assert {version['asset_id'] for version in nodes['scene']['versions']} == {previous, newest}


async def test_plan_mode_cannot_resume_or_stop_even_when_approved(ctx):
    ctx.mode, ctx.approved = 'plan', True
    ctx.manager.store.put('jobs', 'recover', {'id': 'recover', 'account': 'colab2', 'status': 'recovering',
        'creative': {'project_id': 'project', 'canvas_id': 'canvas'}})
    with pytest.raises(ValueError):
        await execute_tool('resume_job', {'job_id': 'recover'}, ctx)
    with pytest.raises(ValueError):
        await execute_tool('server', {'action': 'stop'}, ctx)
    assert not ctx.manager.colab.calls


@pytest.mark.parametrize('data', [{'versions': []}, {'asset_id': None}, {'kind': 'video'}, {'active_version_id': 'anything'},
                                 {'guidance': 7}, {'seed': -1}, {'audio': 'yes'}])
async def test_prompt_updates_cannot_erase_history_or_change_output_identity(ctx, data):
    key = asset(ctx)
    await edit(ctx, [create('a', asset_id=key, media_type='image/png')])
    before = ctx.manager.store.get('projects', 'project')
    with pytest.raises(ValueError):
        await edit(ctx, [{'op': 'update', 'node_id': 'a', 'data': data}], 1)
    assert ctx.manager.store.get('projects', 'project') == before


async def test_removing_group_preserves_child_world_position(ctx):
    group = create('group', 'group')
    group['node']['position'] = {'x': 600, 'y': 300}
    group['node']['style'] = {'width': 600, 'height': 400}
    child = create('a')
    child['node'].update(parentId='group', position={'x': 40, 'y': 60})
    await edit(ctx, [group, child])
    await edit(ctx, [{'op': 'remove', 'node_id': 'group'}], 1)
    node = ctx.manager.store.get('projects', 'project')['canvases'][0]['nodes'][0]
    assert 'parentId' not in node
    assert node['position'] == {'x': 640, 'y': 360}


@pytest.mark.parametrize('operations', [
    [create('a', 'upscale')],
    [create('a'), create('b', 'upscale'), create('c'),
     {'op': 'connect', 'source': 'a', 'target': 'b'}, {'op': 'connect', 'source': 'c', 'target': 'b'}],
    [create('a'), create('b', 'text'), {'op': 'connect', 'source': 'a', 'target': 'b'}],
])
async def test_invalid_input_cardinality_and_noninput_targets_never_persist(ctx, operations):
    with pytest.raises(ValueError):
        await edit(ctx, operations)
    assert ctx.manager.store.get('projects', 'project')['revision'] == 0


async def test_visual_inspection_only_returns_explicit_validated_local_images(ctx):
    key = asset(ctx)
    plain = await execute_tool('inspect_assets', {}, ctx)
    assert '_visual_assets' not in plain
    result = await execute_tool('inspect_assets', {'asset_ids': [key], 'visual_asset_ids': [key]}, ctx)
    assert result['_visual_assets'] == [{'asset_id': key, 'media_type': 'image/png'}]
    with pytest.raises(HTTPException):
        await execute_tool('inspect_assets', {'visual_asset_ids': ['../../anything']}, ctx)
    with pytest.raises(ValueError):
        await execute_tool('inspect_assets', {'visual_asset_ids': [key] * 5}, ctx)


async def test_canvas_creation_is_idempotent_and_has_same_limit_as_project_api(ctx):
    args = {'action': 'create_canvas', 'name': 'Second', 'expected_revision': 0}
    created = await execute_tool('manage_project', args, ctx)
    again = await execute_tool('manage_project', args, ctx)
    assert again == created
    project = ctx.manager.store.get('projects', 'project')
    project['canvases'].extend([{**copy.deepcopy(project['canvases'][0]), 'id': str(i)} for i in range(48)])
    ctx.manager.store.put('projects', 'project', project)
    ctx.call_id = 'new-call'
    with pytest.raises(ValueError):
        await execute_tool('manage_project', {**args, 'expected_revision': 1}, ctx)
    assert len(ctx.manager.store.get('projects', 'project')['canvases']) == 50


async def test_media_replay_reuses_node_despite_stale_revision(ctx, monkeypatch):
    from backend import agent_media
    key = asset(ctx)
    calls = []
    async def extraction(store, *args, **kwargs):
        calls.append(kwargs['result_id'])
        result = {**store.get('assets', key), 'id': kwargs['result_id']}
        source = store.directory / 'assets' / (key + '.png')
        (source.parent / (result['id'] + '.png')).write_bytes(source.read_bytes())
        store.put('assets', result['id'], result)
        return result
    monkeypatch.setattr(agent_media, 'extract', extraction)
    args = {'asset_id': key, 'expected_revision': 0}
    first = await execute_tool('extract_frame', args, ctx)
    await edit(ctx, [create('a')], 1)
    again = await execute_tool('extract_frame', args, ctx)
    assert first['node_id'] == again['node_id']
    assert again['revision'] == 2
    assert len(calls) == 1
    assert len(ctx.manager.store.get('projects', 'project')['canvases'][0]['nodes']) == 2


async def test_real_manager_generates_image_and_reuses_exact_output_for_video(ctx, monkeypatch):
    from backend.agent_media import run_command
    from tests import fake_colab
    fixture_root = ctx.manager.store.directory / 'fake-server'
    fixture = fixture_root / 'tests' / 'fixtures' / 'synthetic.mp4'
    fixture.parent.mkdir(parents=True)
    await run_command(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=green:s=128x128:r=24',
                       '-t', '5', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', fixture])
    monkeypatch.setattr(fake_colab, 'ROOT', fixture_root)
    monkeypatch.setattr(ctx.manager, 'ensure_worker', Manager.ensure_worker.__get__(ctx.manager))
    ctx.manager.colab.delay = .03
    await ctx.manager.open()
    try:
        await edit(ctx, [create('identity'), create('video', 'video', mode='首帧'),
            {'op': 'connect', 'source': 'identity', 'target': 'video', 'role': 'first'}])
        first = await execute_tool('generate_nodes', {'node_ids': ['identity'], 'expected_revision': 1}, ctx)
        result = await execute_tool('get_jobs', {'job_ids': first['job_ids'], 'wait_seconds': 15}, ctx)
        assert result['all_completed'], result['jobs']
        image = ctx.manager.job(first['job_id'])['outputs'][0]['asset_id']
        ctx.call_id = 'video-call'
        second = await execute_tool('generate_nodes', {'node_ids': ['video'], 'expected_revision': result['revision']}, ctx)
        result = await execute_tool('get_jobs', {'job_ids': second['job_ids'], 'wait_seconds': 15}, ctx)
        assert result['all_completed'], result['jobs']
        assert ctx.manager.job(second['job_id'])['creative']['asset_ids'] == [image]
        assert len(ctx.manager.colab.prompt_posts) == 2
        assert len(ctx.manager.colab.uploaded) == 1
        assert ctx.manager.machine(ctx.account)['phase'] == 'ready'
    finally:
        await ctx.manager.close()
