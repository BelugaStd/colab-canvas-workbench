import copy

import pytest

from backend import production
from tests import test_agent_tools
from tests.test_agent_tools import asset, create, edit

ctx = test_agent_tools.ctx


def plan(shots=True):
    return {'brief': {'title': 'Episode one', 'target_seconds': 10, 'conflict': 'Opposed goals', 'arc': 'Choice then reversal'},
            'entities': [{'id': 'hero', 'name': 'Hero', 'kind': 'character', 'description': 'Short black hair, adult, red jacket'},
                         {'id': 'room', 'name': 'Room', 'kind': 'location', 'description': 'Blue room, door right, desk left'}],
            'scenes': [{'id': 'sc1', 'name': 'The meeting', 'location_id': 'room', 'axis': 'hero left, opponent right', 'lighting': 'Blue dusk'}],
            'shots': [{'id': 's1', 'node_id': 'video', 'image_node_id': 'first', 'scene_id': 'sc1', 'duration': 10,
                       'purpose': 'Confrontation', 'action': 'Hero waits', 'camera': {'size': 'MCU', 'lens_mm': 50,
                       'movement': 'slow push', 'motivation': 'reveal tension', 'axis': 'north side', 'eyeline': 'right'},
                       'state_in': {'door': 'closed'}, 'state_out': {'door': 'closed'},
                       'entities': ['hero'], 'bindings': []}] if shots else []}


def test_production_rejects_invalid_duration_state_and_camera():
    original = {'id': 'canvas', 'nodes': [], 'edges': []}
    for mutate in [lambda value: value['shots'][0].update(duration=9),
                   lambda value: value['shots'][0].update(state_out={'door': 'open'}),
                   lambda value: value['shots'][0]['camera'].pop('eyeline'),
                   lambda value: value['shots'].append(copy.deepcopy(value['shots'][0]))]:
        args = plan()
        mutate(args)
        with pytest.raises(ValueError):
            production.save_plan(original, args)
        assert 'production' not in original
    production.save_plan(original, plan())
    assert original['production']['revision'] == 1


def approve(context, canvas, key, purpose, subject=None):
    context.manager.store.put('production_reviews', key + purpose, {'id': key + purpose, 'asset_id': key,
        'canvas_id': canvas['id'], 'purpose': purpose, 'subject': subject, 'verdict': 'pass',
        'review_version': production.REVIEW_VERSION, 'media_hash': production.media_hash(context.manager.store, key),
        'fingerprint': production.fingerprint(canvas, purpose, subject, context.manager.store)})


@pytest.mark.asyncio
async def test_asset_publication_is_exact_reviewed_immutable_version(ctx):
    canvas = ctx.manager.store.get('projects', 'project')['canvases'][0]
    production.save_plan(canvas, plan(False))
    key = asset(ctx)
    args = {'entity_id': 'hero', 'asset_id': key, 'label': 'Night coat'}
    with pytest.raises(ValueError, match='review_media'):
        production.lock_asset(ctx.manager.store, canvas, args)
    approve(ctx, canvas, key, 'master', 'hero')
    lock = production.lock_asset(ctx.manager.store, canvas, args)
    assert production.lock_asset(ctx.manager.store, canvas, args) == lock
    assert len(canvas['production']['locks']) == 1
    mutated = copy.deepcopy(canvas)
    mutated['nodes'][0]['data']['asset_id'] = asset(ctx, 'red')
    with pytest.raises(ValueError, match='不可改变'):
        production.guard_locks(canvas, mutated)
    canvas['production']['entities'][0]['description'] = 'Different person'
    assert not production.approved(ctx.manager.store, canvas, key, 'master', 'hero')


@pytest.mark.asyncio
async def test_generation_requires_actual_master_connections_and_current_still_review(ctx):
    await edit(ctx, [create('first'), create('video', 'video', mode='首帧', duration=10),
                     {'op': 'connect', 'source': 'first', 'target': 'video', 'role': 'first'}])
    project = ctx.manager.store.get('projects', 'project')
    canvas = project['canvases'][0]
    production.save_plan(canvas, plan())
    bindings = []
    for entity in ('hero', 'room'):
        key = asset(ctx)
        approve(ctx, canvas, key, 'master', entity)
        lock = production.lock_asset(ctx.manager.store, canvas, {'entity_id': entity, 'asset_id': key, 'label': 'Approved'})
        bindings.append(lock['id'])
    canvas['production']['shots'][0]['bindings'] = bindings
    with pytest.raises(ValueError, match='实际连线'):
        production.guard_generation(ctx.manager.store, canvas, ['first'])
    for lock in canvas['production']['locks']:
        canvas['edges'].append({'id': lock['id'], 'source': lock['node_id'], 'target': 'first'})
    production.guard_generation(ctx.manager.store, canvas, ['first'])
    with pytest.raises(ValueError, match='精确首尾'):
        production.guard_generation(ctx.manager.store, canvas, ['video'])
    first_key = asset(ctx)
    canvas['nodes'][0]['data']['asset_id'] = first_key
    approve(ctx, canvas, first_key, 'still', 's1')
    production.guard_generation(ctx.manager.store, canvas, ['video'])
    canvas['nodes'][0]['data']['asset_id'] = asset(ctx, 'red')
    with pytest.raises(ValueError, match='精确首尾'):
        production.guard_generation(ctx.manager.store, canvas, ['video'])


@pytest.mark.asyncio
async def test_assembly_never_substitutes_old_reviewed_version_or_reorders_shots(ctx):
    canvas = {'id': 'canvas', 'nodes': [], 'edges': []}
    production.save_plan(canvas, plan())
    key, newer = asset(ctx), asset(ctx, 'red')
    canvas['nodes'].append({'id': 'video', 'data': {'asset_id': key}})
    clips = [{'asset_id': key, 'duration': 10}]
    with pytest.raises(ValueError, match='实际抽帧'):
        production.guard_assembly(ctx.manager.store, canvas, clips)
    approve(ctx, canvas, key, 'shot', 's1')
    production.guard_assembly(ctx.manager.store, canvas, clips)
    canvas['nodes'][0]['data']['asset_id'] = newer
    with pytest.raises(ValueError, match='精确输出版本'):
        production.guard_assembly(ctx.manager.store, canvas, clips)
    assert not production.status(ctx.manager.store, canvas)['complete']
    canvas['nodes'][0]['data']['asset_id'] = key
    canvas['production']['final_asset_id'] = newer
    ctx.manager.store.patch('assets', newer, agent_timeline=clips)
    assert not production.status(ctx.manager.store, canvas)['complete']
    approve(ctx, canvas, newer, 'final')
    assert production.status(ctx.manager.store, canvas)['complete']
