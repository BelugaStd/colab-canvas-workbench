"""Validated canvas-domain tools shared by the conversational Agent."""
import asyncio
import copy
import json
import math
import re
import time
import uuid
from dataclasses import dataclass
from types import SimpleNamespace

from fastapi import HTTPException

from . import agent_media
from .assets import asset_path
from .creative_workflows import ASPECTS, dimensions
from .manager import ACTIVE
from .projects import Generate, submit_canvas, validate_canvases
from .references import reference_plan
from .video_sr_remote import MODEL_PRESETS


@dataclass
class ToolContext:
    manager: object
    project_id: str
    canvas_id: str
    account: str
    run_id: str
    call_id: str
    mode: str = 'auto'
    approved: bool = False


class ToolApprovalRequired(Exception):
    def __init__(self, action, detail='此操作需要用户在 Agent 面板明确批准'):
        super().__init__(detail)
        self.action, self.detail = action, detail


def obj(properties, required=()):
    return {'type': 'object', 'properties': properties, 'required': list(required), 'additionalProperties': False}


def string(maximum=20000):
    return {'type': 'string', 'maxLength': maximum}


def array(items, maximum=100):
    return {'type': 'array', 'items': items, 'maxItems': maximum}


ID = {'type': 'string', 'minLength': 1, 'maxLength': 100}
REVISION = {'type': 'integer', 'minimum': 0}
POSITION = obj({'x': {'type': 'number', 'minimum': -1000000, 'maximum': 1000000},
                'y': {'type': 'number', 'minimum': -1000000, 'maximum': 1000000}}, ('x', 'y'))
DATA = {'type': 'object', 'additionalProperties': True, 'maxProperties': 40}
NODE_DATA = obj({'kind': {'type': 'string', 'enum': ['image', 'video', 'upscale', 'upload', 'text', 'audio', 'group']},
    'label': string(100), 'prompt': string(),
    'model': {'type': 'string', 'enum': ['minimax-image', 'moss-image', 'qwen-edit', 'minimax-h3', 'realesrgan', *MODEL_PRESETS]},
    'aspect': {'type': 'string', 'enum': [*ASPECTS, 'source']},
    'resolution': {'type': 'string', 'enum': ['1K', '2K', '4K', '1MP', '1.5MP', '2MP', '512p', '768p', '2×', '4×']},
    'quality': {'type': 'string', 'enum': ['标准', '精细']},
    'duration': {'type': 'number', 'minimum': 5, 'maximum': 15}, 'fps': {'type': 'integer', 'enum': [24, 30]},
    'audio': {'type': 'boolean'}, 'mode': {'type': 'string', 'enum': ['文生视频', '首帧', '首尾帧', '首尾循环', '参考图']},
    'image_mode': {'type': 'string', 'enum': ['text', 'edit', 'reference']},
    'seed': {'type': 'integer', 'minimum': 0, 'maximum': 2**53 - 1},
    'asset_id': ID, 'media_type': string(100), 'job_id': ID, 'active_version_id': ID,
    'version_selection': {'type': 'string', 'enum': ['manual', 'auto']}, 'pinned': {'type': 'boolean'},
    'reference_sequence': {'type': 'integer', 'minimum': 0},
    'width': {'type': 'number', 'minimum': 1}, 'height': {'type': 'number', 'minimum': 1},
    'versions': array(obj({'id': ID, 'asset_id': ID, 'media_type': string(100), 'created_at': {'type': 'number', 'minimum': 0},
                         'job_id': {'type': ['string', 'null']}, 'settings': DATA, 'note': string(1000)},
                         ('id', 'asset_id', 'media_type', 'created_at')), 1000)})
OPERATION = obj({'op': {'type': 'string', 'enum': ['create', 'update', 'connect', 'disconnect', 'remove', 'layout', 'select_version']},
    'node': obj({'id': ID, 'type': {'type': 'string', 'enum': ['creative']}, 'position': POSITION,
                 'data': NODE_DATA, 'parentId': ID, 'style': DATA}, ('id', 'data')),
    'node_id': ID, 'data': NODE_DATA, 'position': POSITION, 'parent_id': {'type': ['string', 'null'], 'maxLength': 100},
    'source': ID, 'target': ID, 'edge_id': ID, 'role': {'type': 'string', 'enum': ['reference', 'base', 'first', 'last']},
    'alias': {'type': 'string', 'pattern': r'^(图|文本)[1-9]\d{0,3}$'}, 'version_id': ID,
    'positions': {'type': 'object', 'additionalProperties': POSITION, 'maxProperties': 500}}, ('op',))


TOOLS = [
    {'name': 'inspect_canvas', 'description': 'Read current canvas revision, stable node IDs, exact active outputs and connections before edits. Model/mode capabilities are included by default.',
     'parameters': obj({'node_ids': array(ID, 500), 'include_versions': {'type': 'boolean'}, 'include_capabilities': {'type': 'boolean'}})},
    {'name': 'inspect_assets', 'description': 'List local uploaded/generated assets and their URLs, sizes and media types. Use visual_asset_ids to actually inspect up to 4 local images; extract a video frame first. Never accepts file paths or external URLs.',
     'parameters': obj({'asset_ids': array(ID, 100), 'visual_asset_ids': array(ID, 4),
                        'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100}})},
    {'name': 'edit_canvas', 'description': 'Atomically create/update/remove/layout nodes, connect/disconnect typed references and select output versions. Read revision first. For create use node={id,position,data:{kind,label,prompt,model,...}}. Connect uses source,target,role,alias. Changes must form a valid complete graph.',
     'parameters': obj({'expected_revision': REVISION, 'operations': array(OPERATION, 200)}, ('expected_revision', 'operations'))},
    {'name': 'save_story', 'description': 'Save an editable creative brief, identity/style information and ordered shot plan on this canvas. Does not allocate GPU or generate.',
     'parameters': obj({'expected_revision': REVISION, 'brief': DATA, 'characters': array(DATA, 20), 'shots': array(DATA, 120)}, ('expected_revision',))},
    {'name': 'generate_nodes', 'description': 'Run image/video/upscale nodes with exact current references. Completed dependencies are reused. regenerate=false reuses ready requested outputs; true creates fresh versions. Does not wait; returns durable job_ids.',
     'parameters': obj({'node_ids': array(ID, 20), 'expected_revision': REVISION, 'regenerate': {'type': 'boolean'}}, ('node_ids', 'expected_revision'))},
    {'name': 'get_jobs', 'description': 'Wait for/read this project and account generation jobs, then merge completed assets into node history. Wait locally without repeated model calls; all_terminal reports completion/failure/cancellation.',
     'parameters': obj({'job_ids': array(ID, 20), 'wait_seconds': {'type': 'number', 'minimum': 0, 'maximum': 30}, 'wait': {'type': 'boolean'}}, ('job_ids',))},
    {'name': 'cancel_job', 'description': 'Cancel a named generation belonging to the current project/account. Does not delete existing outputs.',
     'parameters': obj({'job_id': ID}, ('job_id',))},
    {'name': 'resume_job', 'description': 'Resume querying a recovering job without resubmitting it.',
     'parameters': obj({'job_id': ID}, ('job_id',))},
    {'name': 'server', 'description': 'Read GPU status/logs, connect and prepare the configured backend, or stop the current account server. Stop requires explicit application approval. Plan mode cannot allocate GPU.',
     'parameters': obj({'action': {'type': 'string', 'enum': ['status', 'connect', 'logs', 'stop']},
                        'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100}}, ('action',))},
    {'name': 'extract_frame', 'description': 'Extract an exact local video frame as a reusable image asset and optional canvas source node. Media operation is idempotent within this tool call.',
     'parameters': obj({'asset_id': ID, 'time_seconds': {'type': 'number', 'minimum': 0, 'maximum': 600},
                        'label': string(100), 'add_to_canvas': {'type': 'boolean'}, 'expected_revision': REVISION}, ('asset_id',))},
    {'name': 'assemble_film', 'description': 'Assemble ordered local video clips into a playable H264/AAC film, trim each clip, normalize fps/size and provide silence where audio is absent. Verify duration/frames; add the final film to the canvas.',
     'parameters': obj({'clips': array(obj({'asset_id': ID, 'start': {'type': 'number', 'minimum': 0},
        'duration': {'type': 'number', 'minimum': .01, 'maximum': 600}}, ('asset_id', 'duration')), 60),
        'name': string(100), 'fps': {'type': 'integer', 'enum': [24, 30, 60]},
        'width': {'type': 'integer', 'minimum': 32, 'maximum': 3840},
        'height': {'type': 'integer', 'minimum': 32, 'maximum': 3840}, 'expected_revision': REVISION}, ('clips', 'expected_revision'))},
    {'name': 'manage_project', 'description': 'Rename the current project/canvas or create/duplicate a canvas inside this project. New canvas ID is returned; the current conversation remains scoped to its canvas.',
     'parameters': obj({'action': {'type': 'string', 'enum': ['rename_project', 'rename_canvas', 'create_canvas', 'duplicate_canvas']},
                        'name': string(100), 'expected_revision': REVISION}, ('action', 'name', 'expected_revision'))},
]


def validate(value, schema, path='arguments'):
    kind = schema.get('type')
    types = kind if isinstance(kind, list) else [kind]
    checks = {'object': lambda: isinstance(value, dict), 'array': lambda: isinstance(value, list),
              'string': lambda: isinstance(value, str), 'number': lambda: type(value) in (int, float) and math.isfinite(value),
              'integer': lambda: type(value) is int, 'boolean': lambda: type(value) is bool, 'null': lambda: value is None}
    if kind and not any(checks[t]() for t in types):
        raise ValueError(f'{path} 类型无效')
    if 'enum' in schema and value not in schema['enum']:
        raise ValueError(f'{path} 不在支持的选项内')
    if isinstance(value, str):
        if not schema.get('minLength', 0) <= len(value) <= schema.get('maxLength', 20000):
            raise ValueError(f'{path} 长度无效')
        if 'pattern' in schema and not re.fullmatch(schema['pattern'], value):
            raise ValueError(f'{path} 格式无效')
    if type(value) in (int, float) and not schema.get('minimum', -math.inf) <= value <= schema.get('maximum', math.inf):
        raise ValueError(f'{path} 数值超出范围')
    if isinstance(value, dict):
        if len(value) > schema.get('maxProperties', 1000) or set(schema.get('required', [])) - value.keys():
            raise ValueError(f'{path} 缺少必需属性或属性过多')
        properties, extra = schema.get('properties', {}), schema.get('additionalProperties', True)
        for key, item in value.items():
            if key not in properties and extra is False:
                raise ValueError(f'{path} 不支持属性 {key}')
            child = properties.get(key, extra if isinstance(extra, dict) else {})
            validate(item, child, path + '.' + key)
    if isinstance(value, list):
        if not schema.get('minItems', 0) <= len(value) <= schema.get('maxItems', 1000):
            raise ValueError(f'{path} 数量无效')
        for item in value:
            validate(item, schema.get('items', {}), path + '[]')


def current(ctx, expected=None):
    project = ctx.manager.store.get('projects', ctx.project_id)
    if not project or project.get('deleted_at') or project.get('kind') != 'project':
        raise HTTPException(404, '当前项目不存在或已在回收站')
    if expected is not None and project['revision'] != expected:
        raise HTTPException(409, '画布已更新，请重新读取 revision 后再修改')
    canvas = next((canvas for canvas in project['canvases'] if canvas['id'] == ctx.canvas_id), None)
    if canvas is None:
        raise HTTPException(404, '当前画布不存在')
    return project, canvas


def commit(ctx, project):
    if len(project['canvases']) > 50:
        raise ValueError('每个项目最多 50 张画布')
    validate_canvases(project['canvases'])
    project.update(revision=project['revision'] + 1, updated_at=time.time())
    ctx.manager.store.put('projects', project['id'], project)
    return project['revision']


def scope_job(ctx, key):
    job = ctx.manager.job(key)
    creative = (job or {}).get('creative', {})
    if (not job or job['account'] != ctx.account or creative.get('project_id') != ctx.project_id
            or creative.get('canvas_id') != ctx.canvas_id):
        raise HTTPException(404, '当前画布没有此任务')
    return job


def versions(data):
    result = copy.deepcopy(data.get('versions', []))
    if not isinstance(result, list) or any(not isinstance(v, dict) or not isinstance(v.get('id'), str)
            or not isinstance(v.get('asset_id'), str) or not isinstance(v.get('media_type'), str)
            or type(v.get('created_at')) not in (int, float) for v in result):
        raise ValueError('节点历史版本格式无效')
    if len({v['id'] for v in result}) != len(result) or len({v['asset_id'] for v in result}) != len(result):
        raise ValueError('节点历史版本 ID 重复')
    if data.get('asset_id') and not any(v['asset_id'] == data['asset_id'] for v in result):
        result.append({'id': data['asset_id'], 'asset_id': data['asset_id'], 'created_at': 0,
                       'media_type': data.get('media_type', 'image/png'), 'job_id': data.get('job_id')})
    return result


def merge_completed_outputs(ctx):
    project, canvas = current(ctx)
    changed = False
    jobs = sorted((job for job in ctx.manager.store.all('jobs') if job.get('status') == 'completed'
                   and job.get('creative', {}).get('project_id') == ctx.project_id
                   and job.get('creative', {}).get('canvas_id') == ctx.canvas_id), key=lambda job: job['created_at'])
    for node in canvas['nodes']:
        data = node['data']
        history, added = versions(data), None
        node_changed = False
        for job in jobs:
            for output in job.get('outputs', []):
                if output.get('creative_node') != node['id'] or not output.get('asset_id'):
                    continue
                asset_path(ctx.manager.store, output['asset_id'])
                existing = next((v for v in history if v['asset_id'] == output['asset_id']), None)
                settings = job['creative'].get('node_settings', {}).get(node['id'])
                if existing:
                    if not existing.get('created_at') or (settings and not existing.get('settings')):
                        existing.update(created_at=job['created_at'], job_id=job['id'])
                        if settings:
                            existing['settings'] = copy.deepcopy(settings)
                        node_changed = True
                    continue
                added = {'id': output['asset_id'], 'asset_id': output['asset_id'], 'job_id': job['id'],
                         'media_type': output.get('media_type', 'image/png'), 'created_at': job['created_at']}
                if settings:
                    added['settings'] = copy.deepcopy(settings)
                history.append(added)
                node_changed = True
        if node_changed:
            history.sort(key=lambda item: item['created_at'])
            data['versions'] = history
            active = next((v for v in history if v['asset_id'] == data.get('asset_id')), {})
            if added and data.get('version_selection') != 'manual' and added['created_at'] >= active.get('created_at', 0):
                data.update(active_version_id=added['id'], version_selection='auto', asset_id=added['asset_id'], media_type=added['media_type'], job_id=added['job_id'])
            changed = True
    if changed:
        commit(ctx, project)
    return project, canvas


def capabilities():
    return {'node_types': ['image', 'video', 'upscale', 'upload', 'text', 'audio', 'group'],
        'images': {'minimax-image': {'resolutions': ['1K', '2K', '4K'], 'max_references': 10},
                   'moss-image': {'resolutions': ['1MP', '1.5MP', '2MP'], 'max_references': 4},
                   'qwen-edit': {'resolutions': ['1MP'], 'min_references': 1, 'max_references': 3}},
        'image_modes': ['text', 'edit', 'reference'], 'video_model': 'minimax-h3',
        'video_modes': {'文生视频': 0, '首帧': 1, '首尾帧': 2, '首尾循环': 1, '参考图': '1–9'},
        'video_duration_seconds': [5, 15], 'video_fps': [24, 30], 'video_resolutions': ['512p', '768p'],
        'quality': ['标准', '精细'], 'aspects': [*ASPECTS, 'source'],
        'upscale_models': ['realesrgan', *MODEL_PRESETS], 'upscale_scales': ['2×', '4×'],
        'reference_roles': ['reference', 'base', 'first', 'last'],
        'reference_aliases': '图1、图2、文本1；提示词用 @图1 引用已连接素材',
        'execution': 'All generation tools use serial per-account GPU queue. Plan mode never allocates GPU.'}


def validate_graph(ctx, canvas):
    nodes = {node['id']: node['data'] for node in canvas['nodes']}
    for node in canvas['nodes']:
        data, kind = node['data'], node['data']['kind']
        if data.get('asset_id'):
            _, asset = asset_path(ctx.manager.store, data['asset_id'])
            data['media_type'] = asset['media_type']
        for version in versions(data):
            asset_path(ctx.manager.store, version['asset_id'])
        if kind == 'image':
            if data.get('model', 'minimax-image') not in {'minimax-image', 'moss-image', 'qwen-edit'}:
                raise ValueError('图片模型只支持 MiniMax Image / MOSS Image / Qwen Edit')
            plan = reference_plan(nodes, canvas['edges'], node['id'])
            model = data.get('model', 'minimax-image')
            if len(plan['images']) > {'minimax-image': 10, 'moss-image': 4, 'qwen-edit': 3}[model]:
                raise ValueError('参考图片数量超过模型上限')
            if data.get('quality', '标准') not in {'标准', '精细'}:
                raise ValueError('图片质量无效')
            dimensions({**data, 'aspect': '1:1' if data.get('aspect') == 'source' else data.get('aspect', '1:1')})
        elif kind == 'video':
            if data.get('model', 'minimax-h3') != 'minimax-h3':
                raise ValueError('视频模型只支持 MiniMax H3')
            if type(data.get('duration', 5)) not in (int, float) or not 5 <= data.get('duration', 5) <= 15:
                raise ValueError('H3 时长支持 5–15 秒')
            if type(data.get('audio', False)) is not bool or data.get('fps', 24) not in (24, 30):
                raise ValueError('视频音频或帧率设置无效')
            reference_plan(nodes, canvas['edges'], node['id'])
            dimensions({**data, 'aspect': '16:9' if data.get('aspect') == 'source' else data.get('aspect', '16:9')}, video=True)
        elif kind == 'upscale':
            if data.get('model', 'seedvr2-7b') not in {'realesrgan', *MODEL_PRESETS} or data.get('resolution', '2×') not in {'2×', '4×'}:
                raise ValueError('超分模型或倍数无效')
            plan = reference_plan(nodes, canvas['edges'], node['id'])
            if len(plan['images']) != 1 or plan['texts']:
                raise ValueError('超分节点需要且仅需要一张图片或一段视频')
        if kind in {'image', 'video', 'upscale'}:
            for edge in canvas['edges']:
                if edge['target'] == node['id'] and nodes[edge['source']]['kind'] in {'audio', 'group'}:
                    raise ValueError('当前模型输入不支持音频或分组')
                if edge['target'] == node['id'] and kind in {'image', 'video'}:
                    source = nodes[edge['source']]
                    if source['kind'] == 'video' or source.get('media_type', '').startswith(('video/', 'audio/')):
                        raise ValueError('当前生成模型需要图片参考')
        elif any(edge['target'] == node['id'] for edge in canvas['edges']):
            raise ValueError('上传、文本、音频和分组节点不接受连线输入')


def edit_canvas(ctx, args):
    project, canvas = current(ctx, args['expected_revision'])
    if not args['operations']:
        raise ValueError('至少提供一个画布操作')
    for operation in args['operations']:
        action = operation['op']
        indexed = {node['id']: node for node in canvas['nodes']}
        node_id = operation.get('node_id')
        if action == 'create':
            new = copy.deepcopy(operation.get('node', {}))
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', new.get('id', '')) or new['id'] in indexed:
                raise ValueError('新节点 ID 无效或重复')
            new.setdefault('type', 'creative')
            new.setdefault('position', {'x': 0, 'y': 0})
            new.setdefault('data', {}).setdefault('label', new['id'])
            new['data'].setdefault('prompt', '')
            canvas['nodes'].append(new)
        elif action == 'connect':
            source, target = operation.get('source'), operation.get('target')
            if source not in indexed or target not in indexed:
                raise ValueError('连线引用不存在的节点')
            edge = {'id': operation.get('edge_id') or 'edge-' + uuid.uuid5(uuid.NAMESPACE_URL, source + ':' + target).hex,
                    'source': source, 'target': target, 'data': {'role': operation.get('role', 'reference')}}
            if 'alias' in operation:
                edge['data']['alias'] = operation['alias']
            canvas['edges'].append(edge)
        elif action == 'disconnect':
            canvas['edges'] = [edge for edge in canvas['edges'] if not (
                edge['id'] == operation.get('edge_id') or (edge['source'] == operation.get('source') and edge['target'] == operation.get('target')))]
        elif action == 'layout':
            for key, position in operation.get('positions', {}).items():
                if key not in indexed:
                    raise ValueError('布局引用不存在的节点')
                indexed[key]['position'] = position
        else:
            if node_id not in indexed:
                raise ValueError('节点不存在')
            target = indexed[node_id]
            if action == 'update':
                changes = operation.get('data', {})
                if set(changes).intersection({'kind', 'versions', 'asset_id', 'media_type', 'job_id', 'active_version_id', 'version_selection'}):
                    raise ValueError('更新参数不能修改节点类型或输出历史；请用 select_version 选择已有输出或新建素材节点')
                target['data'].update(copy.deepcopy(changes))
                if 'position' in operation:
                    target['position'] = operation['position']
                if 'parent_id' in operation:
                    if operation['parent_id'] is None:
                        target.pop('parentId', None)
                    else:
                        target['parentId'] = operation['parent_id']
            elif action == 'remove':
                children = [key for key, value in indexed.items() if value.get('parentId') == node_id]
                for child in children:
                    indexed[child].pop('parentId', None)
                    indexed[child]['position'] = {axis: indexed[child]['position'][axis] + target['position'][axis]
                                                  for axis in ('x', 'y')}
                canvas['nodes'] = [node for node in canvas['nodes'] if node['id'] != node_id]
                canvas['edges'] = [edge for edge in canvas['edges'] if node_id not in (edge['source'], edge['target'])]
            elif action == 'select_version':
                history = versions(target['data'])
                version = next((v for v in history if v['id'] == operation.get('version_id')), None)
                if version is None:
                    raise ValueError('节点版本不存在')
                target['data'].update(versions=history, active_version_id=version['id'], asset_id=version['asset_id'],
                    media_type=version['media_type'], job_id=version.get('job_id'), version_selection='manual')
    validate_canvases(project['canvases'])
    validate_graph(ctx, canvas)
    revision = commit(ctx, project)
    return {'revision': revision, 'node_ids': [node['id'] for node in canvas['nodes']], 'operations_applied': len(args['operations'])}


def job_summary(job):
    fields = ['id', 'account', 'name', 'status', 'message', 'created_at', 'started_at', 'ended_at', 'progress', 'outputs']
    return {key: job[key] for key in fields if key in job}


def require_auto(ctx):
    if ctx.mode == 'plan':
        raise ValueError('当前为规划模式；请切换自动模式后再生成或连接 GPU')


def add_media_node(ctx, asset, label, expected):
    project, canvas = current(ctx)
    key = 'agent-' + uuid.uuid5(uuid.NAMESPACE_URL, f'{ctx.run_id}:{ctx.call_id}:media-node').hex
    existing = next((node for node in canvas['nodes'] if node['id'] == key), None)
    if existing:
        return {'asset': asset, 'asset_id': asset['id'], 'asset_url': '/api/assets/' + asset['id'] + '/file',
                'node_id': key, 'revision': project['revision']}
    current(ctx, expected)
    canvas['nodes'].append({'id': key, 'type': 'creative', 'position': {'x': len(canvas['nodes']) % 8 * 380, 'y': 0},
        'data': {'kind': 'upload', 'label': label, 'prompt': '', 'asset_id': asset['id'], 'media_type': asset['media_type']}})
    revision = commit(ctx, project)
    return {'asset': asset, 'asset_id': asset['id'], 'asset_url': '/api/assets/' + asset['id'] + '/file', 'node_id': key, 'revision': revision}


async def execute_tool(name, args, ctx):
    definition = next((tool for tool in TOOLS if tool['name'] == name), None)
    if definition is None:
        raise ValueError('未知的 Agent 工具')
    if len(json.dumps(args, allow_nan=False)) > 500000:
        raise ValueError('工具参数过大')
    validate(args, definition['parameters'])
    project, canvas = current(ctx)
    if name == 'inspect_canvas':
        wanted = args.get('node_ids')
        nodes = [copy.deepcopy(node) for node in canvas['nodes'] if wanted is None or node['id'] in wanted]
        if wanted is not None and set(wanted) - {node['id'] for node in nodes}:
            raise ValueError('请求的节点不在当前画布')
        if not args.get('include_versions', False):
            for node in nodes:
                node['data'].pop('versions', None)
        return {'project_id': ctx.project_id, 'canvas_id': ctx.canvas_id, 'revision': project['revision'],
                'nodes': nodes, 'edges': canvas['edges'], 'story': canvas.get('agent_story'),
                **({'capabilities': capabilities()} if args.get('include_capabilities', True) else {})}
    if name == 'inspect_assets':
        keys = args.get('asset_ids')
        assets = ([asset_path(ctx.manager.store, key)[1] for key in keys] if keys is not None else
                  sorted(ctx.manager.store.all('assets'), key=lambda item: item.get('created_at', 0), reverse=True)[:args.get('limit', 30)])
        visuals = []
        for key in dict.fromkeys(args.get('visual_asset_ids', [])):
            _, asset = asset_path(ctx.manager.store, key)
            if not asset['media_type'].startswith('image/'):
                raise ValueError('视觉检查需要图片素材；视频请先使用 extract_frame')
            visuals.append({'asset_id': key, 'media_type': asset['media_type']})
        return {'assets': [{**{key: item[key] for key in ('id', 'name', 'media_type', 'width', 'height', 'duration', 'created_at') if key in item},
                             'asset_url': '/api/assets/' + item['id'] + '/file'} for item in assets],
                **({'_visual_assets': visuals} if visuals else {})}
    if name == 'edit_canvas':
        return edit_canvas(ctx, args)
    if name == 'save_story':
        project, canvas = current(ctx, args['expected_revision'])
        story = {**canvas.get('agent_story', {}), **{key: args[key] for key in ('brief', 'characters', 'shots') if key in args}}
        for shot in story.get('shots', []):
            if 'duration' in shot:
                agent_media.number(shot['duration'], '镜头时长', 0.1, 600)
        canvas['agent_story'] = story
        return {'revision': commit(ctx, project), 'story': story}
    if name == 'generate_nodes':
        require_auto(ctx)
        if not args['node_ids'] or len(set(args['node_ids'])) != len(args['node_ids']):
            raise ValueError('请指定 1–20 个不同的生成节点')
        # WARN: Job metadata is written by Manager.submit before any GPU worker can start.
        existing = [job for job in ctx.manager.store.all('jobs') if job.get('creative', {}).get('agent_run_id') == ctx.run_id
                    and job.get('creative', {}).get('agent_call_id') == ctx.call_id]
        if existing:
            for job in existing:
                scope_job(ctx, job['id'])
            return {'job_id': existing[0]['id'], 'job_ids': [job['id'] for job in existing], 'reused_nodes': [],
                    'revision': project['revision'], 'reconciled': True}
        current(ctx, args['expected_revision'])
        project, canvas = merge_completed_outputs(ctx)
        indexed = {node['id']: node for node in canvas['nodes']}
        if set(args['node_ids']) - indexed.keys():
            raise ValueError('生成节点不在当前画布')
        reuse = [key for key in args['node_ids'] if indexed[key]['data'].get('asset_id') and not args.get('regenerate', False)]
        targets = [key for key in args['node_ids'] if key not in reuse]
        if not targets:
            return {'job_id': None, 'job_ids': [], 'reused_nodes': reuse, 'revision': project['revision']}
        def submit(account, graph, label, stop_after, creative):
            creative = {**creative, 'agent_run_id': ctx.run_id, 'agent_call_id': ctx.call_id}
            return ctx.manager.submit(account, graph, label, stop_after, creative=creative)
        proxy = SimpleNamespace(profiles=ctx.manager.profiles, submit=submit)
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(store=ctx.manager.store, manager=proxy)))
        job = submit_canvas(ctx.project_id, ctx.canvas_id, targets, Generate(account=ctx.account), request)
        if args.get('regenerate', False) and any(indexed[key]['data'].get('version_selection') == 'manual' for key in targets):
            for key in targets:
                indexed[key]['data']['version_selection'] = 'auto'
            commit(ctx, project)
        return {'job_id': job['id'], 'job_ids': [job['id']], 'status': job['status'], 'reused_nodes': reuse, 'revision': project['revision']}
    if name == 'get_jobs':
        if not args['job_ids']:
            raise ValueError('至少提供一个任务 ID')
        jobs = [scope_job(ctx, key) for key in args['job_ids']]
        deadline = time.monotonic() + (args.get('wait_seconds', 30) if args.get('wait', True) else 0)
        while any(job['status'] in ACTIVE for job in jobs) and time.monotonic() < deadline:
            await asyncio.sleep(min(.5, max(0, deadline - time.monotonic())))
            jobs = [scope_job(ctx, key) for key in args['job_ids']]
        project, _ = merge_completed_outputs(ctx)
        return {'job_ids': args['job_ids'], 'jobs': [job_summary(job) for job in jobs], 'revision': project['revision'],
                'all_terminal': all(job['status'] not in ACTIVE for job in jobs),
                'all_completed': all(job['status'] == 'completed' for job in jobs)}
    if name in {'cancel_job', 'resume_job'}:
        if name == 'resume_job':
            require_auto(ctx)
        scope_job(ctx, args['job_id'])
        job = await ctx.manager.cancel(args['job_id']) if name == 'cancel_job' else ctx.manager.resume(args['job_id'])
        return {'job': job_summary(job), 'job_ids': [job['id']]}
    if name == 'server':
        action = args['action']
        if action == 'logs':
            return {'logs': ctx.manager.store.get('logs', ctx.account, [])[-args.get('limit', 30):]}
        if action == 'connect':
            require_auto(ctx)
            ctx.manager.request_start(ctx.account, full=True)
        if action == 'stop':
            require_auto(ctx)
            if not ctx.approved:
                raise ToolApprovalRequired('server.stop')
            ctx.manager.request_stop(ctx.account)
        machine = ctx.manager.machine(ctx.account)
        return {'account': ctx.account, 'machine': {key: machine.get(key) for key in ('phase', 'session', 'message', 'stats', 'deployment')},
                'usage': ctx.manager.store.get('accounts', ctx.account, {}).get('usage')}
    if name in {'extract_frame', 'assemble_film'}:
        require_auto(ctx)
        add = name == 'assemble_film' or args.get('add_to_canvas', True)
        expected = args.get('expected_revision')
        if add and expected is None:
            raise ValueError('添加媒体节点需要 expected_revision')
        result_id = uuid.uuid5(uuid.NAMESPACE_URL, f'workbench-agent:{ctx.run_id}:{ctx.call_id}:{name}').hex
        existing = ctx.manager.store.get('assets', result_id)
        if existing:
            asset_path(ctx.manager.store, result_id)
            if add:
                return add_media_node(ctx, existing, existing['name'], expected)
            return {'asset': existing, 'asset_id': existing['id'], 'asset_url': '/api/assets/' + existing['id'] + '/file'}
        if add:
            current(ctx, expected)
        if name == 'extract_frame':
            label = args.get('label', '提取帧')
            asset = await agent_media.extract(ctx.manager.store, args['asset_id'], args.get('time_seconds', 0), label, result_id=result_id)
        else:
            label = args.get('name', '成片')
            asset = await agent_media.assemble(ctx.manager.store, args['clips'], label, args.get('fps', 24),
                args.get('width', 1280), args.get('height', 720), result_id=result_id)
        if add:
            return add_media_node(ctx, asset, label, expected)
        return {'asset': asset, 'asset_id': asset['id'], 'asset_url': '/api/assets/' + asset['id'] + '/file'}
    if name == 'manage_project':
        action = args['action']
        canvas_key = uuid.uuid5(uuid.NAMESPACE_URL, ctx.run_id + ':' + ctx.call_id + ':canvas').hex
        if action in {'create_canvas', 'duplicate_canvas'} and any(item['id'] == canvas_key for item in project['canvases']):
            return {'revision': project['revision'], 'project_id': ctx.project_id, 'canvas_id': canvas_key}
        project, canvas = current(ctx, args['expected_revision'])
        if not args['name'].strip():
            raise ValueError('名称不能为空')
        if action == 'rename_project':
            project['name'] = args['name']
        elif action == 'rename_canvas':
            canvas['name'] = args['name']
        else:
            new = copy.deepcopy(canvas) if action == 'duplicate_canvas' else {'nodes': [], 'edges': [], 'viewport': {'x': 0, 'y': 0, 'zoom': 1}}
            new.update(id=canvas_key, name=args['name'])
            project['canvases'].append(new)
            canvas = new
        return {'revision': commit(ctx, project), 'project_id': ctx.project_id, 'canvas_id': canvas['id']}
    raise ValueError('工具尚未实现')
