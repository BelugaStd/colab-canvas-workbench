"""Resolve visible reference aliases to deterministic model input slots."""
import re


def reference_plan(nodes, edges, key):
    sources, roles, aliases, image_count, text_count = [], {}, {}, 0, 0
    for edge in edges:
        if edge['target'] != key:
            continue
        source = edge['source']
        if source in sources:
            raise ValueError('同一个来源只需连接一次，请删除重复引用')
        sources.append(source)
        text = nodes[source]['kind'] == 'text'
        image_count += not text
        text_count += text
        alias = edge.get('data', {}).get('alias') or f"{'文本' if text else '图'}{text_count if text else image_count}"
        if not re.fullmatch(r'(?:图|文本)[1-9]\d{0,3}', alias) or alias in aliases.values():
            raise ValueError('引用编号无效或重复，请重新连接素材')
        aliases[source] = alias
        role = edge.get('data', {}).get('role', 'reference')
        if role not in {'reference', 'base', 'first', 'last'}:
            raise ValueError('未知的参考用途')
        if text and role != 'reference':
            raise ValueError('文本引用不能作为主图或首尾帧')
        roles[source] = role
    images = [s for s in sources if nodes[s]['kind'] != 'text']
    texts = [s for s in sources if nodes[s]['kind'] == 'text']
    data = nodes[key]
    mode = data.get('mode')
    if data['kind'] == 'video':
        mode = mode or ('文生视频' if not images else '首帧' if len(images) == 1 else '首尾帧')
        required = {'文生视频': 0, '首帧': 1, '首尾帧': 2, '首尾循环': 1}
        if mode == '参考图':
            if not 1 <= len(images) <= 9:
                raise ValueError('参考图模式需要 1–9 张图片')
            if any(roles[s] != 'reference' for s in images):
                raise ValueError('参考图模式不固定首尾帧，请将图片用途改为参考')
        elif mode not in required:
            raise ValueError('请选择文生视频、首帧、首尾帧、首尾循环或参考图模式')
        elif len(images) != required[mode]:
            raise ValueError(f'{mode}模式需要 {required[mode]} 张图片，当前连接了 {len(images)} 张；请选择匹配的模式或调整连线')
        else:
            if any(roles[s] == 'base' for s in images):
                raise ValueError('视频图片用途应为首帧、尾帧或参考')
            for role in ('first', 'last'):
                if sum(roles[s] == role for s in images) > 1:
                    raise ValueError('首帧和尾帧各只能指定一张图片')
            if mode != '首尾帧' and any(roles[s] == 'last' for s in images):
                raise ValueError('尾帧图片需要使用首尾帧模式')
            images.sort(key=lambda s: {'first': 0, 'reference': 1, 'last': 2}[roles[s]])
            for i, source in enumerate(images):
                roles[source] = 'last' if i else 'first'
    elif data['kind'] == 'image':
        if data.get('model') == 'qwen-edit' and not 1 <= len(images) <= 3:
            raise ValueError('Qwen Edit 需要连接 1–3 张图片；文生图请选择 MiniMax 或 MOSS')
        if any(roles[s] in {'first', 'last'} for s in images):
            raise ValueError('图片生成请将输入用途设为主图或参考图')
        if sum(roles[s] == 'base' for s in images) > 1:
            raise ValueError('图像编辑只能选择一张主图')
        images.sort(key=lambda s: roles[s] != 'base')
        if images:
            roles[images[0]] = 'base'
        mode = data.get('image_mode', 'edit' if images else 'text')
        if mode not in {'text', 'edit', 'reference'}:
            raise ValueError('请选择文生图、编辑主图或参考生图')
        if (mode == 'text' and images) or (mode != 'text' and not images):
            raise ValueError('图片模式与连线不匹配，请连接图片或切换模式')
    return {'images': images, 'texts': texts, 'aliases': aliases, 'roles': roles, 'mode': mode}


def resolve_prompt(nodes, key, plan):
    prompt = nodes[key].get('prompt', '').strip()
    labels, names = {}, {}
    for source in plan['images'] + plan['texts']:
        label = nodes[source].get('label', '')
        if label:
            labels.setdefault(label, []).append(source)
        names[plan['aliases'][source]] = [source]
    names = {**labels, **names}
    used_text = set()
    pattern = '|'.join(re.escape('@' + name) + (r'(?!\d)' if re.fullmatch(r'(?:图|文本)\d+', name) else r'(?![A-Za-z0-9_.-])' if name[-1:].isascii() else '')
                       for name in sorted(names, key=len, reverse=True))

    def expand(value, stack=()):
        replacements = []
        def replace(match):
            name = match.group()[1:]
            sources = names[name]
            if len(sources) != 1:
                raise ValueError(f'引用 @{name} 对应多个同名节点，请使用图片或文本编号重新插入引用')
            source = sources[0]
            if source in plan['texts']:
                if source in stack:
                    raise ValueError('文本引用存在循环，请删除相互引用')
                used_text.add(source)
                result = expand(nodes[source].get('prompt', '').strip(), (*stack, source))
            else:
                number = plan['images'].index(source) + 1
                result = f'Picture {number}' if nodes[key].get('model') in {'moss-image', 'qwen-edit'} else f'<Picture {number}>'
            replacements.append(result)
            return f'\x00{len(replacements)-1}\x00'
        escaped = value.replace('@@', '\x01')
        if pattern:
            escaped = re.sub(pattern, replace, escaped)
        missing = re.search(r'@([^\s，。,;；:：!?！？<>]+)', escaped)
        if missing:
            raise ValueError(f'引用 @{missing[1]} 未连接或已更名，请从已连接素材重新插入引用')
        return re.sub(r'\x00(\d+)\x00', lambda m: replacements[int(m[1])], escaped).replace('\x01', '@')

    prompt = expand(prompt)
    for source in plan['texts']:
        if source not in used_text:
            used_text.add(source)
            prompt += '\n' + expand(nodes[source].get('prompt', '').strip(), (source,))
    if not prompt.strip() and nodes[key]['kind'] != 'upscale':
        raise ValueError(f"请先填写「{nodes[key]['label']}」的提示词或连接文本节点")
    if nodes[key]['kind'] == 'image' and plan['images'] and nodes[key].get('image_mode'):
        picture = 'Picture 1' if nodes[key].get('model') in {'moss-image', 'qwen-edit'} else '<Picture 1>'
        if plan['mode'] == 'edit':
            prompt = f'Edit {picture}. {prompt.strip()}\nPreserve the original visual medium and style, all content, identity, composition and background not explicitly requested to change. 保持原图的绘画或摄影类型、视觉风格，以及未要求修改的内容不变。'
        elif plan['mode'] == 'reference':
            prompt = f'Create a new image using the provided pictures as references according to this instruction: {prompt.strip()}'
    return prompt.strip()
