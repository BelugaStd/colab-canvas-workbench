"""Versioned production records and enforceable continuity boundaries."""
import copy
import hashlib
import json
import math
import re
import time
from collections import Counter
from functools import lru_cache

from .assets import asset_path
from .references import reference_plan

REVIEW_VERSION = 'production-review-v2'
BUDGET_MAX_SAMPLE_AGE_SECONDS = 60


@lru_cache(maxsize=256)
def file_hash(path, size, modified):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def media_hash(store, asset_id):
    path, _ = asset_path(store, asset_id)
    stat = path.stat()
    return file_hash(str(path), stat.st_size, stat.st_mtime_ns)


def identity(value, name):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', value):
        raise ValueError(name + ' 必须是稳定的短 ID')
    return value


def text(value, name, limit=4000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(name + ' 缺失或过长')
    return value


def index(items, name, maximum):
    if not isinstance(items, list) or len(items) > maximum:
        raise ValueError(name + ' 列表无效或过多')
    result = {}
    for item in items:
        if not isinstance(item, dict):
            raise ValueError(name + ' 记录无效')
        key = identity(item.get('id'), name)
        if key in result:
            raise ValueError(name + ' ID 重复')
        result[key] = item
    return result


def fingerprint(canvas, purpose, subject=None, store=None):
    production = canvas.get('production', {})
    if purpose == 'master':
        value = next((item for item in production.get('entities', []) if item['id'] == subject), None)
    elif purpose in {'still', 'shot'}:
        value = next((item for item in production.get('shots', []) if item['id'] == subject), None)
        if value:
            scene = next((item for item in production.get('scenes', []) if item['id'] == value['scene_id']), None)
            selected_locks = [lock for lock in production.get('locks', []) if lock['id'] in value['bindings']]
            value = {'shot': value, 'scene': scene, 'brief': production.get('brief'),
                     'locks': selected_locks}
    else:
        value = {key: production.get(key) for key in ('brief', 'entities', 'scenes', 'shots', 'locks')}
        nodes = {node['id']: node['data'] for node in canvas['nodes']}
        value['selected_outputs'] = [{'shot_id': shot['id'], 'asset_id': nodes.get(shot['node_id'], {}).get('asset_id')}
                                    for shot in production.get('shots', [])]
        if store:
            for item in value['selected_outputs']:
                if item['asset_id']:
                    item['media_hash'] = media_hash(store, item['asset_id'])
    if production and value is None:
        raise ValueError('制作台账中找不到检查对象')
    encoded = json.dumps(value or {}, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def approved(store, canvas, asset_id, purpose, subject=None):
    expected = fingerprint(canvas, purpose, subject, store)
    return any(item.get('asset_id') == asset_id and item.get('purpose') == purpose
               and item.get('subject') == subject and item.get('fingerprint') == expected
               and item.get('canvas_id') == canvas['id'] and item.get('verdict') == 'pass'
               and item.get('review_version') == REVIEW_VERSION
               and item.get('media_hash') == media_hash(store, asset_id)
               for item in store.all('production_reviews'))


def save_plan(canvas, args):
    old = canvas.get('production', {})
    result = {**copy.deepcopy(old), **{key: copy.deepcopy(args[key]) for key in ('brief', 'entities', 'scenes', 'shots') if key in args}}
    brief = result.get('brief', {})
    text(brief.get('title'), '剧集标题', 100)
    target = brief.get('target_seconds')
    if type(target) not in (int, float) or not math.isfinite(target) or not 5 <= target <= 600:
        raise ValueError('目标时长必须为 5–600 秒')
    text(brief.get('conflict'), '核心矛盾')
    text(brief.get('arc'), '本集推进与结尾钩子')
    entities = index(result.get('entities', []), '人物场景道具', 60)
    for item in entities.values():
        if item.get('kind') not in {'character', 'location', 'prop'}:
            raise ValueError('资产类型须为 character/location/prop')
        text(item.get('name'), '资产名称', 100)
        text(item.get('description'), '资产不变量')
    locks = index(result.get('locks', []), '已锁版本', 200)
    for lock in locks.values():
        if lock['entity_id'] not in entities:
            raise ValueError('不能删除已有锁定版本所属的资产')
    scenes = index(result.get('scenes', []), '场次', 40)
    for scene in scenes.values():
        if entities.get(scene.get('location_id'), {}).get('kind') != 'location':
            raise ValueError('场次必须引用已定义场景资产')
        text(scene.get('name'), '场次名称', 100)
        text(scene.get('axis'), '场次轴线')
        text(scene.get('lighting'), '场次光线')
    shots = index(result.get('shots', []), '镜头', 120)
    node_ids = set()
    for shot in shots.values():
        scene = scenes.get(shot.get('scene_id'))
        if not scene:
            raise ValueError('镜头须引用已定义场次')
        for field in ('node_id', 'image_node_id'):
            identity(shot.get(field), field)
        if shot['node_id'] in node_ids:
            raise ValueError('视频节点不能同时属于两个镜头')
        node_ids.add(shot['node_id'])
        duration = shot.get('duration')
        start = shot.get('start', 0)
        source_duration = shot.get('source_duration', max(5, duration) if type(duration) in (int, float) else 0)
        if (type(duration) not in (int, float) or not math.isfinite(duration) or not 1 / 24 <= duration <= 15
                or type(source_duration) not in (int, float) or not math.isfinite(source_duration)
                or type(start) not in (int, float) or not math.isfinite(start) or start < 0
                or not 5 <= source_duration <= 15 or duration + start > source_duration):
            raise ValueError('剪辑镜头须在 5–15 秒 H3 原片范围内，明确 source_duration 与裁切 start')
        for field in ('purpose', 'action'):
            text(shot.get(field), '镜头' + field)
        camera = shot.get('camera', {})
        for field in ('size', 'movement', 'motivation', 'axis', 'eyeline'):
            text(camera.get(field), '摄影' + field, 1500)
        if type(camera.get('lens_mm')) not in (int, float) or not 14 <= camera['lens_mm'] <= 200:
            raise ValueError('焦段须为 14–200mm 的明确数值')
        for field in ('state_in', 'state_out'):
            if not isinstance(shot.get(field), dict) or not shot[field]:
                raise ValueError('每镜需明确非空入场与出场连续性状态')
        required = shot.get('entities', [])
        if not isinstance(required, list) or any(key not in entities for key in required):
            raise ValueError('镜头资产引用无效')
        for line in shot.get('dialogue', []):
            if (not isinstance(line, dict) or entities.get(line.get('speaker'), {}).get('kind') != 'character'
                    or not entities[line['speaker']].get('voice')):
                raise ValueError('每句计划对白须对应已定义且固定声线的角色')
            text(line.get('text'), '对白', 1000)
        bindings = shot.get('bindings', [])
        if not isinstance(bindings, list) or len(set(bindings)) != len(bindings) or any(key not in locks for key in bindings):
            raise ValueError('镜头只能绑定明确存在且不重复的锁定版本')
        if shot['state_in'] != shot['state_out'] and not shot.get('end_image_node_id'):
            raise ValueError('状态变化镜头需明确 end_image_node_id，以首尾帧约束状态')
        if shot.get('end_image_node_id'):
            identity(shot['end_image_node_id'], '尾帧节点')
    if shots and abs(sum(shot['duration'] for shot in shots.values()) - target) > .001:
        raise ValueError('镜头时长之和须等于目标总时长')
    ordered = list(shots.values())
    for left, right in zip(ordered, ordered[1:]):
        if left['scene_id'] == right['scene_id'] and not right.get('transition'):
            common = left['state_out'].keys() & right['state_in'].keys()
            if any(left['state_out'][key] != right['state_in'][key] for key in common):
                raise ValueError('同场相邻镜头状态不连续；明确实际可见的过渡或保持出入场状态一致')
    result.update(schema_version=1, revision=old.get('revision', 0) + 1)
    canvas['production'] = result
    return result


def lock_asset(store, canvas, args):
    production = canvas.get('production')
    if not production:
        raise ValueError('先 save_production 定义资产，再锁定版本')
    entity = next((item for item in production['entities'] if item['id'] == args['entity_id']), None)
    if not entity:
        raise ValueError('未知人物、场景或道具')
    key = args['asset_id']
    _, asset = asset_path(store, key)
    if not asset['media_type'].startswith('image/'):
        raise ValueError('视觉资产版本只能锁定实际图片')
    if not approved(store, canvas, key, 'master', entity['id']):
        raise ValueError('锁定前必须通过该资产当前设定的独立 review_media 实图检查')
    locks = production.setdefault('locks', [])
    existing = next((item for item in locks if item['entity_id'] == entity['id'] and item['asset_id'] == key), None)
    if existing:
        return existing
    version = sum(item['entity_id'] == entity['id'] for item in locks) + 1
    lock_id = f"{entity['id']}-v{version}"
    node_id = 'locked-' + lock_id
    if any(node['id'] == node_id for node in canvas['nodes']):
        raise ValueError('锁定快照节点 ID 被占用')
    lock = {'id': lock_id, 'entity_id': entity['id'], 'label': text(args.get('label'), '版本名称', 100),
            'asset_id': key, 'node_id': node_id, 'continuity': args.get('continuity', {}),
            'entity_snapshot': copy.deepcopy(entity), 'media_hash': media_hash(store, key), 'created_at': time.time()}
    locks.append(lock)
    canvas['nodes'].append({'id': node_id, 'type': 'creative',
        'position': {'x': -500, 'y': 280 * len(locks)},
        'data': {'kind': 'upload', 'label': entity['name'] + ' · ' + lock_id, 'prompt': '',
                 'asset_id': key, 'media_type': asset['media_type'], 'production_lock': lock_id}})
    return lock


def guard_locks(before, after):
    nodes = {node['id']: node for node in after['nodes']}
    for lock in before.get('production', {}).get('locks', []):
        data = nodes.get(lock['node_id'], {}).get('data', {})
        if data.get('asset_id') != lock['asset_id'] or data.get('production_lock') != lock['id']:
            raise ValueError('已发布人物/场景版本不可改变或删除；请锁定新版本并显式更改镜头绑定')


def shot_references(canvas, shot):
    locks = {item['id']: item for item in canvas['production'].get('locks', [])}
    required = set(shot.get('entities', []))
    scene = next(item for item in canvas['production']['scenes'] if item['id'] == shot['scene_id'])
    required.add(scene['location_id'])
    selected = [locks[key] for key in shot.get('bindings', [])]
    if required - {lock['entity_id'] for lock in selected}:
        raise ValueError('镜头须显式绑定全部出镜人物、场景和关键道具的锁定版本')
    return selected


def guard_generation(store, canvas, targets):
    if not canvas.get('production'):
        return
    guard_locks(canvas, canvas)
    for lock in canvas['production'].get('locks', []):
        if media_hash(store, lock['asset_id']) != lock['media_hash']:
            raise ValueError('已锁资产文件发生变化，不能继续使用旧版本')
    nodes = {node['id']: node for node in canvas['nodes']}
    def ancestors(key, seen=None):
        seen = set() if seen is None else seen
        for edge in canvas['edges']:
            if edge['target'] == key and edge['source'] not in seen:
                seen.add(edge['source'])
                ancestors(edge['source'], seen)
        return seen
    for shot in canvas['production'].get('shots', []):
        ids = {shot['node_id'], shot['image_node_id'], shot.get('end_image_node_id')}
        relevant = ids.intersection(targets)
        if not relevant:
            continue
        locks = shot_references(canvas, shot)
        for key in relevant:
            parents = ancestors(key)
            if any(lock['node_id'] not in parents for lock in locks):
                raise ValueError('镜头实际连线必须追溯到绑定的原始锁定资产，不能只靠提示词或 seed')
            if key != shot['node_id']:
                actual = reference_plan({node_id: node['data'] for node_id, node in nodes.items()}, canvas['edges'], key)
                if any(lock['node_id'] not in actual['images'] for lock in locks):
                    raise ValueError('静帧必须直接引用绑定的原始锁定素材；上一镜输出不能替代人物或场景参考')
        if shot['node_id'] in relevant:
            data = nodes[shot['node_id']]['data']
            if data.get('duration') != shot.get('source_duration', max(5, shot['duration'])):
                raise ValueError('视频时长与已保存镜头计划不一致')
            endpoints = [shot['image_node_id']]
            if shot.get('end_image_node_id'):
                endpoints.append(shot['end_image_node_id'])
                if data.get('mode') != '首尾帧':
                    raise ValueError('状态变化必须使用实际首尾帧模式')
            for endpoint in endpoints:
                asset_id = nodes.get(endpoint, {}).get('data', {}).get('asset_id')
                if not asset_id or not approved(store, canvas, asset_id, 'still', shot['id']):
                    raise ValueError('视频前需对当前精确首尾静帧通过独立 review_media 检查')
            actual = reference_plan({key: node['data'] for key, node in nodes.items()}, canvas['edges'], shot['node_id'])
            raw_roles = {edge['source']: edge.get('data', {}).get('role') for edge in canvas['edges'] if edge['target'] == shot['node_id']}
            expected_roles = {endpoint: ('first' if index == 0 else 'last') for index, endpoint in enumerate(endpoints)}
            if (actual['images'] != endpoints or actual['mode'] != ('首尾帧' if len(endpoints) == 2 else '首帧')
                    or any(raw_roles.get(key) != role for key, role in expected_roles.items())):
                raise ValueError('视频实际首尾帧节点与用途必须精确等于已审核镜头计划')


def budget(store, account):
    ledger = store.get('production_budgets', account)
    if not ledger:
        return None
    record = store.get('accounts', account, {})
    usage = record.get('usage', {})
    balance = usage.get('balance')
    values = (ledger.get('balance_before'), ledger.get('limit_cu'),
              ledger.get('last_balance', ledger.get('balance_before')), ledger.get('gross_used_cu', 0))
    if any(type(value) not in (int, float) or not math.isfinite(value) or value < 0 for value in values):
        raise ValueError('预算额度尚未读取，不能推测剩余额度')
    def valid(value):
        return type(value) in (int, float) and math.isfinite(value) and value >= 0
    known_balance = valid(balance)
    used = ledger.get('gross_used_cu', 0)
    before = copy.deepcopy(ledger)
    if known_balance:
        # WARN: Credits or refunds must not reset spending; estimates never enter this actual-use accumulator.
        used += max(0, ledger.get('last_balance', ledger['balance_before']) - balance)
        ledger.update(last_balance=balance, gross_used_cu=used)
    checked_at = record.get('usage_checked_at')
    if checked_at is None and not record.get('error'):
        checked_at = record.get('checked_at')
    if known_balance and not record.get('error') and valid(checked_at) and checked_at > 0:
        ledger['last_usage_checked_at'] = checked_at
        if valid(usage.get('rate')):
            ledger['last_rate_cu_per_hour'] = usage['rate']
    checked_at = ledger.get('last_usage_checked_at')
    age = max(0, time.time() - checked_at) if valid(checked_at) and checked_at > 0 else None
    rate = ledger.get('last_rate_cu_per_hour')
    error = ledger.get('quota_error') or record.get('error')
    stale = age is None or age > BUDGET_MAX_SAMPLE_AGE_SECONDS
    unknown = not known_balance or stale or bool(error)
    estimate = used + rate * age / 3600 if valid(rate) and age is not None else None
    if ledger != before:
        store.put('production_budgets', account, ledger)
    return {'authorization_id': ledger['authorization_id'], 'limit_cu': ledger['limit_cu'],
            'gross_used_cu': round(used, 3), 'remaining_cu': round(max(0, ledger['limit_cu'] - used), 3),
            'estimated_used_cu': round(estimate, 3) if estimate is not None else None,
            'estimated_remaining_cu': round(max(0, ledger['limit_cu'] - estimate), 3) if estimate is not None else None,
            'quota_status': 'unknown' if unknown else 'fresh', 'stale': stale, 'quota_error': error,
            'usage_checked_at': checked_at, 'sample_age_seconds': age, 'last_rate_cu_per_hour': rate,
            'shutdown_status': ledger.get('shutdown_status'), 'shutdown_error': ledger.get('shutdown_error'),
            'balance_floor': ledger['balance_before'] - ledger['limit_cu']}


def guard_budget(store, account):
    value = budget(store, account)
    if not value:
        return
    if value['remaining_cu'] <= 5 or (value['estimated_remaining_cu'] is not None and value['estimated_remaining_cu'] <= 5):
        raise ValueError('本轮共享预算已到预留底线，不能通过新项目重置授权或继续新增推理')
    if value['quota_status'] == 'unknown' or value['shutdown_status']:
        raise ValueError('本轮预算额度陈旧或未知，或已触发预算停机，禁止新增推理；请先核对官方额度与授权')


def guard_assembly(store, canvas, clips):
    production = canvas.get('production')
    if not production:
        return
    shots = production.get('shots', [])
    nodes = {node['id']: node['data'] for node in canvas['nodes']}
    if not shots or len(clips) != len(shots):
        raise ValueError('正式合片须一一对应已保存的完整镜头表')
    for shot, clip in zip(shots, clips, strict=True):
        asset_id = nodes.get(shot['node_id'], {}).get('asset_id')
        if (clip.get('asset_id') != asset_id or abs(clip.get('duration', 0) - shot['duration']) > .001
                or abs(clip.get('start', 0) - shot.get('start', 0)) > .001):
            raise ValueError('合片顺序、精确输出版本或时长不符合镜头表')
        if not approved(store, canvas, asset_id, 'shot', shot['id']):
            raise ValueError('每个当前视频版本均须通过独立实际抽帧 review_media，不能用旧版审片结果')


def guard_sound(store, canvas, source_asset_id, cues):
    production = canvas.get('production')
    if not production:
        return
    if source_asset_id != production.get('final_asset_id'):
        raise ValueError('正式配音只能基于当前完整镜头表的成片，不能把无关单镜提升为交付')
    characters = {item['id']: item for item in production['entities'] if item['kind'] == 'character'}
    for cue in cues:
        if cue['role'] != 'dialogue':
            continue
        character = characters.get(cue.get('speaker'))
        speech = asset_path(store, cue['asset_id'])[1].get('agent_speech', {})
        if not character or not character.get('voice') or character['voice'] != speech.get('speaker'):
            raise ValueError('正式对白必须匹配制作台账已锁定的角色与声线')


def deliverable_source(store, canvas, final_id):
    visited = set()
    key = final_id
    while key and key not in visited and len(visited) < 20:
        visited.add(key)
        _, asset = asset_path(store, key)
        if asset.get('agent_timeline'):
            try:
                guard_assembly(store, canvas, asset['agent_timeline'])
                return True
            except ValueError:
                return False
        key = asset.get('sound_timeline', {}).get('video_asset_id')
    return False


def status(store, canvas):
    production = canvas.get('production')
    if not production:
        return None
    nodes = {node['id']: node['data'] for node in canvas['nodes']}
    shots = []
    for shot in production.get('shots', []):
        asset_id = nodes.get(shot['node_id'], {}).get('asset_id')
        shots.append({'id': shot['id'], 'node_id': shot['node_id'], 'duration': shot['duration'],
                      'asset_id': asset_id, 'approved': bool(asset_id and approved(store, canvas, asset_id, 'shot', shot['id']))})
    final = production.get('final_asset_id')
    sound_ready = True
    if final and production['brief'].get('dialogue_required'):
        final_asset = asset_path(store, final)[1]
        cues = [cue for cue in final_asset.get('sound_timeline', {}).get('cues', []) if cue.get('role') == 'dialogue']
        characters = {item['id']: item for item in production.get('entities', []) if item['kind'] == 'character'}
        planned = Counter((line.get('speaker'), line.get('text')) for shot in production.get('shots', [])
                          for line in shot.get('dialogue', []))
        actual = Counter((cue.get('speaker'), cue.get('text')) for cue in cues)
        sound_ready = bool(cues and planned and planned == actual
                           and all(cue.get('speaker') in characters and cue.get('voice')
                                   and cue['voice'] == characters[cue['speaker']].get('voice') for cue in cues)
                           and final_asset.get('sound_validation', {}).get('full_decode'))
    source_valid = bool(final and deliverable_source(store, canvas, final))
    return {'target_seconds': production['brief']['target_seconds'], 'locks': len(production.get('locks', [])),
            'shots': shots, 'final_asset_id': final,
            'source_valid': source_valid, 'sound_ready': sound_ready,
            'complete': bool(shots and all(item['approved'] for item in shots) and final
                             and sound_ready and source_valid
                             and approved(store, canvas, final, 'final'))}
