"""Independent regression audits for production approval boundaries; no model or GPU calls."""
import copy
import json
import uuid
from types import SimpleNamespace

import pytest

from backend import agent_review, production
from backend.agent_tools import execute_tool
from backend.projects import ProjectSave, save_project
from tests import test_agent_tools
from tests.test_agent_tools import asset, create, edit
from tests.test_production import approve, plan

ctx = test_agent_tools.ctx


def persist(ctx, canvas):
    project = ctx.manager.store.get('projects', ctx.project_id)
    project['canvases'][0] = canvas
    ctx.manager.store.put('projects', ctx.project_id, project)


def publish_masters(ctx, canvas):
    bindings = []
    for entity in ('hero', 'room'):
        key = asset(ctx)
        approve(ctx, canvas, key, 'master', entity)
        lock = production.lock_asset(ctx.manager.store, canvas, {'entity_id': entity, 'asset_id': key, 'label': 'Approved'})
        bindings.append(lock['id'])
    canvas['production']['shots'][0]['bindings'] = bindings


def test_changing_scene_contract_invalidates_prior_shot_approval(ctx):
    canvas = ctx.manager.store.get('projects', 'project')['canvases'][0]
    production.save_plan(canvas, plan())
    video = asset(ctx)
    approve(ctx, canvas, video, 'shot', 's1')
    updated = copy.deepcopy(canvas['production'])
    updated['scenes'][0]['lighting'] = 'Bright red daylight with the hero in a hard backlight'
    updated['scenes'][0]['axis'] = 'Reversed: hero right, opponent left'
    production.save_plan(canvas, updated)
    assert not production.approved(ctx.manager.store, canvas, video, 'shot', 's1'), 'A changed scene must invalidate visual approval'


def test_changing_final_clip_version_requires_new_assembly_and_final_review(ctx):
    canvas = ctx.manager.store.get('projects', 'project')['canvases'][0]
    production.save_plan(canvas, plan())
    old, new, final = asset(ctx), asset(ctx, 'red'), asset(ctx, 'green')
    ctx.manager.store.patch('assets', final, agent_timeline=[{'asset_id': old, 'duration': 10, 'start': 0}])
    canvas['nodes'] = [{'id': 'video', 'data': {'asset_id': old}}]
    canvas['production']['final_asset_id'] = final
    approve(ctx, canvas, old, 'shot', 's1')
    approve(ctx, canvas, final, 'final')
    assert production.status(ctx.manager.store, canvas)['complete']
    canvas['nodes'][0]['data']['asset_id'] = new
    approve(ctx, canvas, new, 'shot', 's1')
    assert not production.status(ctx.manager.store, canvas)['complete'], 'The old final still contains the old clip version'


@pytest.mark.asyncio
async def test_generation_rejects_an_unreviewed_actual_video_input(ctx):
    first_key, wrong_key = asset(ctx), asset(ctx, 'red')
    await edit(ctx, [create('first', asset_id=first_key), create('wrong-first', asset_id=wrong_key),
                     create('video', 'video', mode='首帧', duration=10),
                     {'op': 'connect', 'source': 'wrong-first', 'target': 'video', 'role': 'first'}])
    canvas = ctx.manager.store.get('projects', 'project')['canvases'][0]
    production.save_plan(canvas, plan())
    publish_masters(ctx, canvas)
    for lock in canvas['production']['locks']:
        for target in ('first', 'wrong-first'):
            canvas['edges'].append({'id': lock['id'] + '-' + target, 'source': lock['node_id'], 'target': target})
    approve(ctx, canvas, first_key, 'still', 's1')
    persist(ctx, canvas)
    with pytest.raises(ValueError, match='首|尾|输入|连线|endpoint|reference'):
        await execute_tool('generate_nodes', {'node_ids': ['video'], 'expected_revision': 1}, ctx)
    assert ctx.manager.store.all('jobs') == []


@pytest.mark.asyncio
async def test_still_cannot_replace_original_locks_with_only_a_previous_shot_output(ctx):
    from backend.creative_workflows import compile_canvas

    previous_asset = asset(ctx, 'red')
    await edit(ctx, [create('previous-shot', asset_id=previous_asset, media_type='image/png'), create('first'),
                     {'op': 'connect', 'source': 'previous-shot', 'target': 'first', 'role': 'reference'}])
    canvas = ctx.manager.store.get('projects', ctx.project_id)['canvases'][0]
    production.save_plan(canvas, plan())
    publish_masters(ctx, canvas)
    for lock in canvas['production']['locks']:
        canvas['edges'].append({'id': lock['id'], 'source': lock['node_id'], 'target': 'previous-shot'})

    compiled = compile_canvas(canvas, 'first')
    assert compiled['reference_plans']['first']['images'] == ['previous-shot']
    assert compiled['asset_ids'] == [previous_asset], 'The real compiler reuses the completed previous image and does not load its ancestors'
    assert not {lock['asset_id'] for lock in canvas['production']['locks']}.intersection(compiled['asset_ids'])
    with pytest.raises(ValueError, match='原始|锁|直接|参考'):
        production.guard_generation(ctx.manager.store, canvas, ['first'])


@pytest.mark.asyncio
async def test_normal_project_save_cannot_remove_a_published_snapshot(ctx):
    canvas = ctx.manager.store.get('projects', 'project')['canvases'][0]
    production.save_plan(canvas, plan(False))
    publish_masters_plan = canvas['production']
    key = asset(ctx)
    approve(ctx, canvas, key, 'master', 'hero')
    production.lock_asset(ctx.manager.store, canvas, {'entity_id': 'hero', 'asset_id': key, 'label': 'Approved'})
    persist(ctx, canvas)
    changed = ctx.manager.store.get('projects', 'project')
    changed['canvases'][0]['nodes'] = []
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(store=ctx.manager.store)))
    assert publish_masters_plan['locks']
    with pytest.raises(ValueError, match='锁|不可|版本'):
        await save_project('project', ProjectSave(**changed), request)


@pytest.mark.asyncio
async def test_final_review_observes_every_planned_shot_not_six_sparse_frames(ctx, monkeypatch):
    canvas = ctx.manager.store.get('projects', 'project')['canvases'][0]
    args = plan()
    args['brief']['target_seconds'] = 60
    args['shots'] = [{**copy.deepcopy(args['shots'][0]), 'id': f's{index}', 'node_id': f'video-{index}',
                      'image_node_id': f'image-{index}', 'duration': 5} for index in range(12)]
    production.save_plan(canvas, args)
    persist(ctx, canvas)
    video, frame = asset(ctx), asset(ctx, 'red')
    ctx.manager.store.patch('assets', video, media_type='video/mp4', duration=60)
    clips = []
    for shot in canvas['production']['shots']:
        key = asset(ctx)
        canvas['nodes'].append({'id': shot['node_id'], 'data': {'asset_id': key}})
        approve(ctx, canvas, key, 'shot', shot['id'])
        clips.append({'asset_id': key, 'duration': shot['duration']})
    ctx.manager.store.patch('assets', video, agent_timeline=clips)
    canvas['production']['final_asset_id'] = video
    persist(ctx, canvas)
    moments = []

    async def probe(_):
        return {'format': {'duration': '60'}, 'streams': [{'codec_type': 'video', 'codec_name': 'h264', 'avg_frame_rate': '24/1', 'nb_frames': '1440'}]}

    async def extract(store, asset_id, moment, *args, **kwargs):
        moments.append(moment)
        return store.get('assets', frame)

    async def sheet(store, keys, *args):
        return store.get('assets', frame)

    async def complete(*args, **kwargs):
        return {'text': json.dumps({'verdict': 'pass', 'checks': [
            {'category': category, 'passed': True, 'evidence': 'Observed the provided frames'}
            for category in ('identity', 'continuity', 'action', 'camera', 'artifacts')],
            'summary': 'All supplied evidence passes', 'limitations': 'Only supplied stills were observed'}), 'usage': {}}

    monkeypatch.setattr(agent_review.agent_media, 'probe', probe)
    monkeypatch.setattr(agent_review.agent_media, 'extract', extract)
    monkeypatch.setattr(agent_review, 'evidence_sheet', sheet)
    monkeypatch.setattr(agent_review, 'complete', complete)
    await agent_review.review(ctx, {'asset_id': video, 'purpose': 'final'}, None, {}, lambda usage: None)
    unseen = [index for index in range(12) if not any(index * 5 <= moment < (index + 1) * 5 for moment in moments)]
    assert not unseen, f'Final approval skipped entire planned shots: {unseen}; sampled times: {moments}'


def test_manifest_rejects_negative_source_trim_start(ctx):
    canvas = ctx.manager.store.get('projects', 'project')['canvases'][0]
    args = plan()
    args['shots'][0]['start'] = -4
    with pytest.raises(ValueError, match='start|裁切|范围'):
        production.save_plan(canvas, args)


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid_evidence', [None, [], {}])
async def test_independent_review_requires_actual_text_evidence(ctx, monkeypatch, invalid_evidence):
    canvas = ctx.manager.store.get('projects', 'project')['canvases'][0]
    production.save_plan(canvas, plan(False))
    persist(ctx, canvas)
    key = asset(ctx)

    async def complete(*args, **kwargs):
        return {'text': json.dumps({'verdict': 'pass', 'checks': [
            {'category': category, 'passed': True, 'evidence': invalid_evidence}
            for category in ('identity', 'continuity', 'action', 'camera', 'artifacts')]}), 'usage': {}}

    monkeypatch.setattr(agent_review, 'complete', complete)
    with pytest.raises(agent_review.ReviewUnavailable, match='质检|完整|证据'):
        await agent_review.review(ctx, {'asset_id': key, 'purpose': 'master', 'entity_id': 'hero'}, None, {}, lambda usage: None)
    assert ctx.manager.store.all('production_reviews') == []


@pytest.mark.asyncio
async def test_shot_reviewer_receives_actual_scene_contract(ctx, monkeypatch):
    canvas = ctx.manager.store.get('projects', 'project')['canvases'][0]
    production.save_plan(canvas, plan())
    publish_masters(ctx, canvas)
    first = asset(ctx)
    canvas['nodes'].append({'id': 'first', 'data': {'asset_id': first}})
    persist(ctx, canvas)
    captured = []

    async def complete(http, config, history, *args, **kwargs):
        captured.append(json.loads(history[0]['content']))
        return {'text': json.dumps({'verdict': 'reject', 'checks': [
            {'category': category, 'passed': False, 'evidence': 'Incomplete supplied contract'}
            for category in ('identity', 'continuity', 'action', 'camera', 'artifacts')]}), 'usage': {}}

    async def sheet(store, keys, *args):
        return store.get('assets', first)

    monkeypatch.setattr(agent_review, 'complete', complete)
    monkeypatch.setattr(agent_review, 'evidence_sheet', sheet)
    await agent_review.review(ctx, {'asset_id': first, 'purpose': 'still', 'shot_id': 's1'}, None, {}, lambda usage: None)
    assert captured[0].get('scene') == canvas['production']['scenes'][0], 'Scene axis and lighting must reach the independent reviewer'


def test_editing_an_unrelated_prop_does_not_invalidate_this_shot_review(ctx):
    canvas = ctx.manager.store.get('projects', 'project')['canvases'][0]
    args = plan()
    args['entities'].append({'id': 'future-prop', 'kind': 'prop', 'name': 'Future prop', 'description': 'Not visible in this shot'})
    production.save_plan(canvas, args)
    video = asset(ctx)
    approve(ctx, canvas, video, 'shot', 's1')
    changed = copy.deepcopy(canvas['production'])
    changed['entities'][-1]['description'] = 'Updated for a different shot in a later scene'
    production.save_plan(canvas, changed)
    assert production.approved(ctx.manager.store, canvas, video, 'shot', 's1'), 'Unrelated future assets must not force expensive re-review'


@pytest.mark.asyncio
async def test_sound_mix_cannot_promote_an_unrelated_clip_to_the_final_film(ctx, monkeypatch):
    from backend import agent_sound
    canvas = ctx.manager.store.get('projects', 'project')['canvases'][0]
    production.save_plan(canvas, plan())
    assembled, unrelated, mixed = asset(ctx), asset(ctx, 'red'), asset(ctx, 'green')
    for key in (assembled, unrelated, mixed):
        ctx.manager.store.patch('assets', key, media_type='video/mp4', duration=10)
    canvas['production']['final_asset_id'] = assembled
    persist(ctx, canvas)
    called = []

    async def mix(*args, **kwargs):
        called.append(args)
        return ctx.manager.store.get('assets', mixed)

    monkeypatch.setattr(agent_sound, 'mix', mix)
    with pytest.raises(ValueError):
        await execute_tool('mix_sound', {'expected_revision': 0, 'video_asset_id': unrelated, 'cues': []}, ctx)
    assert not called, 'A wrong production source must be rejected before media work starts'


@pytest.mark.asyncio
async def test_final_review_cannot_approve_wrong_technical_duration(ctx, monkeypatch):
    canvas = ctx.manager.store.get('projects', 'project')['canvases'][0]
    production.save_plan(canvas, plan())
    persist(ctx, canvas)
    video, frame = asset(ctx), asset(ctx, 'red')
    ctx.manager.store.patch('assets', video, media_type='video/mp4', duration=3)

    async def probe(_):
        return {'format': {'duration': '3'}, 'streams': [{'codec_type': 'video', 'codec_name': 'h264', 'avg_frame_rate': '24/1', 'nb_frames': '72'}]}

    async def extract(store, *args, **kwargs):
        return store.get('assets', frame)

    async def sheet(store, *args):
        return store.get('assets', frame)

    async def complete(*args, **kwargs):
        return {'text': json.dumps({'verdict': 'pass', 'checks': [
            {'category': category, 'passed': True, 'evidence': 'Looks acceptable in supplied frames'}
            for category in ('identity', 'continuity', 'action', 'camera', 'artifacts')]}), 'usage': {}}

    monkeypatch.setattr(agent_review.agent_media, 'probe', probe)
    monkeypatch.setattr(agent_review.agent_media, 'extract', extract)
    monkeypatch.setattr(agent_review, 'evidence_sheet', sheet)
    monkeypatch.setattr(agent_review, 'complete', complete)
    with pytest.raises((ValueError, RuntimeError)):
        await agent_review.review(ctx, {'asset_id': video, 'purpose': 'final'}, None, {}, lambda usage: None)
    assert ctx.manager.store.all('production_reviews') == []


@pytest.mark.asyncio
@pytest.mark.parametrize('has_saved_record', [True, False], ids=['durable-result', 'no-durable-result'])
async def test_review_recovery_reuses_only_real_saved_results_and_never_repays(ctx, monkeypatch, has_saved_record):
    from backend.agent_service import AgentService
    service = AgentService(ctx.manager)
    session = {'id': 'session', 'project_id': ctx.project_id, 'canvas_id': ctx.canvas_id}
    run = {'id': ctx.run_id, 'session_id': session['id'], 'account': ctx.account, 'mode': 'auto',
           'status': 'running', 'events': [], 'usage': {}}
    args = {'asset_id': 'reviewed-output', 'purpose': 'final'}
    call = {'id': ctx.call_id, 'name': 'review_media', 'arguments': json.dumps(args)}
    ledger = {'id': 'ledger', 'run_id': ctx.run_id, 'call_id': ctx.call_id, 'name': 'review_media',
              'status': 'started', 'arguments': args}
    service.store.put('agent_runs', ctx.run_id, run)
    service.store.put('agent_calls', 'ledger', ledger)
    saved_id = uuid.uuid5(uuid.NAMESPACE_URL, f'production-review:{ctx.run_id}:{ctx.call_id}').hex
    saved = {'id': saved_id, 'project_id': ctx.project_id, 'canvas_id': ctx.canvas_id,
             'asset_id': args['asset_id'], 'purpose': 'final', 'verdict': 'pass',
             'review_version': production.REVIEW_VERSION, 'checks': [], 'summary': 'Durably completed before restart'}
    if has_saved_record:
        service.store.put('production_reviews', saved_id, saved)
    invoked = []

    async def replay(name, arguments, context):
        invoked.append(name)
        return {'id': 'saved-review', 'verdict': 'pass', 'cached_review': True}

    monkeypatch.setattr(service, 'invoke_tool', replay)
    result = await service.call_tool(run, session, call, args, 'ledger', ledger)
    assert not invoked, 'An uncertain paid review must never be resent automatically'
    if has_saved_record:
        assert result == saved, 'Recovery must reuse the actual durably saved review result'
    else:
        assert result.get('uncertain'), 'Missing durable evidence must remain uncertain rather than paying twice'
    assert service.store.get('agent_calls', 'ledger')['result'] == result


def test_required_dialogue_cannot_be_satisfied_by_ambience_only(ctx):
    canvas = ctx.manager.store.get('projects', 'project')['canvases'][0]
    args = plan()
    args['brief']['dialogue_required'] = True
    args['entities'][0]['voice'] = 'Serena'
    args['shots'][0]['dialogue'] = [{'speaker': 'hero', 'text': 'I will keep the light on.'}]
    production.save_plan(canvas, args)
    clip, assembled, mixed, ambience = asset(ctx), asset(ctx, 'red'), asset(ctx, 'green'), asset(ctx, 'yellow')
    canvas['nodes'] = [{'id': 'video', 'data': {'asset_id': clip}}]
    ctx.manager.store.patch('assets', assembled, agent_timeline=[{'asset_id': clip, 'duration': 10, 'start': 0}])
    ctx.manager.store.patch('assets', mixed, sound_timeline={'video_asset_id': assembled, 'cues': [
        {'asset_id': ambience, 'start': 0, 'role': 'ambience'}]}, sound_validation={'full_decode': True})
    canvas['production']['final_asset_id'] = mixed
    approve(ctx, canvas, clip, 'shot', 's1')
    approve(ctx, canvas, mixed, 'final')
    status = production.status(ctx.manager.store, canvas)
    assert status['source_valid']
    assert not status['sound_ready'], 'An ambience track does not deliver the explicitly required spoken dialogue'
    assert not status['complete']
