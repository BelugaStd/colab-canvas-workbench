"""Compile creative cards to pinned native ComfyUI graphs. No user-supplied class names."""
import json
import math
import secrets

from .config import ROOT
from .references import reference_plan, resolve_prompt
from .video_sr_remote import MODEL_PRESETS, output_dimensions

ASPECTS = {"16:9": 16 / 9, "3:2": 1.5, "4:3": 4 / 3, "1:1": 1, "3:4": .75, "9:16": 9 / 16}


def dimensions(data, video=False, source_size=None):
    aspect = data.get("aspect", "16:9" if video else "1:1")
    if aspect == 'source' and source_size:
        ratio = source_size[0] / source_size[1]
    elif aspect == 'source':
        raise ValueError('无法读取主图尺寸，请重新连接有效图片或选择画幅')
    elif aspect not in ASPECTS:
        raise ValueError("请选择支持的画幅")
    else:
        ratio = ASPECTS[aspect]
    if not .25 <= ratio <= 4:
        raise ValueError('源图片画幅过窄或过宽，请选择 1:1、16:9 等输出画幅')
    resolution = data.get("resolution", "768p" if video else "1MP" if data.get('model') in {'moss-image', 'qwen-edit'} else "2K")
    if video:
        if resolution not in {"512p", "768p"}:
            raise ValueError("H3 视频仅提供推荐范围内的 512p / 768p")
        short = 512 if resolution == "512p" else 768
        width, height = (short * ratio, short) if ratio >= 1 else (short, short / ratio)
        scale = min(1, math.sqrt(768 * 1344 / (width * height)))
        width, height = width * scale, height * scale
        width, height = max(256, round(width/32)*32), max(256, round(height/32)*32)
        while width*height > 768*1344:
            if width/height >= ratio:
                width -= 32
            else:
                height -= 32
        return width,height
    if data.get('model') == 'moss-image':
        areas = {'1K': 1, '2K': 2, '1MP': 1, '1.5MP': 1.5, '2MP': 2}
        if resolution not in areas:
            raise ValueError('MOSS 图片分辨率可选 1MP / 1.5MP / 2MP')
        area = areas[resolution] * 1024**2
    elif data.get('model') == 'qwen-edit':
        if resolution != '1MP':
            raise ValueError('Qwen Edit 使用推荐的 1MP 分辨率')
        area = 1024**2
    elif resolution not in {"1K", "2K"}:
        raise ValueError("图片分辨率须为 1K 或 2K")
    else:
        area = 1024**2 if resolution == "1K" else 2048**2
    multiple = 16 if data.get('model') == 'moss-image' else 32
    return round(math.sqrt(area * ratio) / multiple) * multiple, round(math.sqrt(area / ratio) / multiple) * multiple


def compile_canvas(canvas, target_id, asset_info=None):
    targets = [target_id] if isinstance(target_id, str) else list(dict.fromkeys(target_id))
    nodes = {n["id"]: n["data"] for n in canvas["nodes"]}
    if not targets or any(target not in nodes for target in targets):
        raise ValueError("节点不存在")
    for edge in canvas["edges"]:
        if edge["source"] not in nodes or edge["target"] not in nodes:
            raise ValueError("连线引用了不存在的节点")
    graph, outputs, assets, models, resolved, visiting = {}, {}, [], set(), {}, set()
    postprocess = {}
    postprocess_nodes = {}
    asset_media_types, output_types = {}, {}
    node_models = {}
    reference_plans, node_dimensions, node_seeds = {}, {}, {}
    asset_info = asset_info or {}

    def add(class_type, **inputs):
        key = f"c{len(graph) + 1}"
        graph[key] = {"class_type": class_type, "inputs": inputs}
        return [key, 0]

    def build(key, force=False):
        if key in visiting:
            raise ValueError("不能创建循环参考")
        if key in resolved:
            return resolved[key]
        data, kind = nodes[key], nodes[key]["kind"]
        if kind == "text":
            return None
        visiting.add(key)
        if data.get("asset_id") and not force:
            media_type = data.get("media_type", "image/png")
            if not media_type.startswith(("image/", "video/")):
                raise ValueError("请连接图片或视频素材")
            asset_id = data["asset_id"]
            if asset_id not in assets:
                assets.append(asset_id)
            asset_media_types[asset_id] = media_type
            info = asset_info.get(asset_id, data)
            if info.get('width') and info.get('height'):
                node_dimensions[key] = (info['width'], info['height'])
            if media_type.startswith("video/"):
                extension = "webm" if media_type == "video/webm" else "mp4"
                result = add("LoadVideo", file=f"creative-{asset_id}.{extension}")
                output_types[key] = "video"
            else:
                result = add("LoadImage", image=f"creative-{asset_id}.png")
                output_types[key] = "image"
        elif kind == "upload":
            raise ValueError(f"请先给「{data['label']}」上传素材")
        elif kind in {"image", "video", "upscale"}:
            plan = reference_plan(nodes, canvas['edges'], key)
            reference_plans[key] = plan
            reference_ids = plan['images']
            references = [build(source, force=source in targets) for source in reference_ids]
            if kind != "upscale" and any(output_types[source] != "image" for source in reference_ids):
                raise ValueError("当前生成模型使用图片参考，请连接图片输出；视频可连接超分节点")
            prompt = resolve_prompt(nodes, key, plan)
            plan['resolved_prompt'] = prompt
            seed = data.get("seed", secrets.randbits(32))
            if type(seed) is not int or not 0 <= seed < 2**53:
                raise ValueError("随机种子无效")
            node_seeds[key] = seed
            size = node_dimensions.get(reference_ids[0]) if reference_ids else None
            effective_data = data
            if data.get('aspect') == 'source' and not reference_ids:
                effective_data = {**data, 'aspect': '16:9' if kind == 'video' else '1:1'}
            if kind == "image":
                width, height = dimensions(effective_data, source_size=size)
                node_dimensions[key] = (width, height)
                if data.get("quality", "标准") not in {"标准", "精细"}:
                    raise ValueError("请选择标准或精细质量")
                model = data.get("model", "minimax-image")
                if model == "minimax-image":
                    if len(references) > 10:
                        raise ValueError("MiniMax Image 最多使用 10 张参考图")
                    models.add("minimax-image")
                    unet = add("UNETLoader", unet_name="minimax_h3_image_pruned_int8_convrot.safetensors", weight_dtype="default")
                    clip = add("CLIPLoader", clip_name="qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors", type="minimax", device="default")
                    vae = add("VAELoader", vae_name="minimax_h3_image_vae_fp16.safetensors")
                    positive = add("MiniMaxH3ImageConditioning", clip=clip, vae=vae, prompt=prompt, width=width, height=height,
                                   **{f"images.image_{i}": ref for i, ref in enumerate(references)})
                    latent = [positive[0], 1]
                    negative = add("ConditioningZeroOut", conditioning=positive)
                    steps, cfg, sampler, scheduler = 6, 1.0, "euler", "simple"
                elif model == "moss-image":
                    models.add("moss-image")
                    unet = add("UNETLoader", unet_name="moss_image_sft_int8_convrot.safetensors", weight_dtype="default")
                    clip = add("CLIPLoader", clip_name="qwen3.5_9b_w4a8_gptq.safetensors", type="moss_image", device="default")
                    vae = add("VAELoader", vae_name="flux2-vae_small.safetensors")
                    latent = add("EmptyFlux2LatentImage", width=width, height=height, batch_size=1)
                    negative_prompt = "worst quality, bad anatomy, watermark, jpeg artifact"
                    if references:
                        models.add("moss-edit")
                        unet = add("LoraLoaderModelOnly", model=unet, lora_name="moss_image_edit_lora_rank_64_bf16.safetensors", strength_model=1.0)
                        unet = add('SkipLayerGuidanceDiTSimple', model=unet, double_layers='', single_layers='22', start_percent=0.0, end_percent=1.0)
                        if len(references) > 4:
                            raise ValueError("MOSS Image 最多使用 4 张参考图")
                        ref_inputs = {f"images.image_{i}": add("ImageScaleToTotalPixels", image=ref, upscale_method="lanczos", megapixels=width * height / 1024**2 if i == 0 else 1.0, resolution_steps=16)
                                      for i, ref in enumerate(references)}
                        positive = add("TextEncodeMOSSImageEdit", clip=clip, vae=vae, prompt=prompt,
                                       edit_format="Moss-Image-Edit", image_strength=1.0, **ref_inputs)
                        negative = add("TextEncodeMOSSImageEdit", clip=clip, vae=vae, prompt=negative_prompt,
                                       edit_format="Moss-Image-Edit", image_strength=1.0, **ref_inputs)
                        steps, cfg, sampler, scheduler = 30, 4.0, "euler", "normal"
                    else:
                        positive = add("CLIPTextEncode", clip=clip, text=prompt)
                        negative = add("CLIPTextEncode", clip=clip, text=negative_prompt)
                        steps, cfg, sampler, scheduler = 25, 2.5, "er_sde", "simple"
                elif model == 'qwen-edit':
                    models.add('qwen-edit')
                    unet = add('UNETLoader', unet_name='qwen_image_edit_2511_int8_convrot.safetensors', weight_dtype='default')
                    unet = add('ModelSamplingAuraFlow', model=unet, shift=3.1)
                    unet = add('CFGNorm', model=unet, strength=1.0)
                    clip = add('CLIPLoader', clip_name='qwen_2.5_vl_7b_fp8_scaled.safetensors', type='qwen_image', device='default')
                    vae = add('VAELoader', vae_name='qwen_image_vae.safetensors')
                    main_image = add('ImageScale', image=references[0], upscale_method='lanczos', width=width, height=height, crop='center')
                    ref_inputs = {f'image{i+1}': ref for i, ref in enumerate([main_image, *references[1:]])}
                    positive = add('TextEncodeQwenImageEditPlus', clip=clip, vae=vae, prompt=prompt, **ref_inputs)
                    negative = add('TextEncodeQwenImageEditPlus', clip=clip, vae=vae, prompt='', **ref_inputs)
                    latent = add('VAEEncode', pixels=main_image, vae=vae)
                    steps, cfg, sampler, scheduler = 40, 4.0, 'euler', 'simple'
                else:
                    raise ValueError("图片模型只支持 MiniMax Image / MOSS Image / Qwen Edit")
                sampled = add("KSampler", model=unet, positive=positive, negative=negative, latent_image=latent,
                              seed=seed, steps=steps, cfg=cfg, sampler_name=sampler, scheduler=scheduler, denoise=1.0)
                result = add("VAEDecode", samples=sampled, vae=vae)
                if data.get("quality") == "精细":
                    models.add("upscale")
                    upscaler = add("UpscaleModelLoader", model_name="RealESRGAN_x4plus.pth")
                    enhanced = add("ImageUpscaleWithModel", upscale_model=upscaler, image=result)
                    result = add("ImageScale", image=enhanced, upscale_method="lanczos", width=width, height=height, crop="disabled")
                saved = add("SaveImage", images=result, filename_prefix="creative")
            elif kind == "video":
                width, height = dimensions(effective_data, video=True, source_size=size)
                node_dimensions[key] = (width, height)
                duration, fps = data.get("duration", 5), data.get("fps", 24)
                if type(duration) not in (int, float) or not 5 <= duration <= 15 or fps not in (24, 30):
                    raise ValueError("H3 支持 5–15 秒，输出帧率可选 24 / 30")
                mode = plan['mode']
                models.add('minimax-h3-reference' if mode == '参考图' else 'minimax-h3')
                template = json.loads((ROOT / "templates/h3-int8.json").read_text())
                mapping = {old: f"c{len(graph) + index + 1}" for index, old in enumerate(template)}
                for old, node in template.items():
                    graph[mapping[old]] = {"class_type": node["class_type"], "inputs": {
                        name: [mapping[value[0]], value[1]] if isinstance(value, list) else value
                        for name, value in node["inputs"].items()}}
                inputs = graph[mapping["5"]]["inputs"]
                length = 5 + 17 * math.ceil((duration * 24 - 5) / 17)
                inputs.update(prompt=prompt, width=width, height=height, length=length)
                if mode == '参考图':
                    graph[mapping['1']]['inputs']['unet_name'] = 'minimax_h3_ref2va_pruned_int8_convrot.safetensors'
                    graph[mapping['5']]['class_type'] = 'MiniMaxH3ReferenceToVideo'
                    inputs.update(ref_image_size='match', **{f'ref_images.ref_image_{i}': ref for i, ref in enumerate(references)})
                scaled = [] if mode == '参考图' else [add("ImageScale", image=ref, upscale_method="lanczos", width=width, height=height, crop="center") for ref in references]
                if scaled:
                    inputs["first_frame"] = scaled[0]
                    if mode == "首尾循环":
                        inputs["last_frame"] = scaled[0]
                    elif mode == "首尾帧" and len(scaled) > 1:
                        inputs["last_frame"] = scaled[1]
                graph[mapping["11"]]["inputs"]["noise_seed"] = seed
                if not data.get("audio", False):
                    del graph[mapping["14"]]
                    del graph[mapping["15"]]["inputs"]["audio"]
                saved, result = [mapping["16"], 0], [mapping["15"], 0]
                postprocess_nodes[key] = {"fps": fps, "duration": duration, "loop": mode == "首尾循环"}
                if key == targets[0]:
                    postprocess.update(fps=fps, duration=duration, loop=mode == "首尾循环")
            else:
                if len(references) != 1:
                    raise ValueError("超分节点需要连接一张图片或一段视频")
                scale = data.get("resolution", "2×")
                if scale not in {"2×", "4×"}:
                    raise ValueError("超分倍数可选 2× / 4×")
                if output_types[reference_ids[0]] == "video":
                    model = data.get("model", "seedvr2-7b")
                    if model == "realesrgan":
                        model = "seedvr2-7b"
                    if model not in MODEL_PRESETS:
                        raise ValueError("请选择支持的视频超分模型")
                    models.add(model)
                    node_models[key] = model
                    result = add("WorkbenchVideoUpscale", video=references[0], model=model, scale=int(scale[0]))
                    saved = add("SaveVideo", video=result, filename_prefix="video/creative-upscale", format="mp4", codec="h264")
                    if reference_ids[0] in postprocess_nodes:
                        postprocess_nodes[key] = dict(postprocess_nodes[reference_ids[0]])
                else:
                    models.add("upscale")
                    node_models[key] = "realesrgan"
                    upscaler = add("UpscaleModelLoader", model_name="RealESRGAN_x4plus.pth")
                    result = add("ImageUpscaleWithModel", upscale_model=upscaler, image=references[0])
                    if scale == "2×":
                        result = add("ImageScaleBy", image=result, upscale_method="lanczos", scale_by=.5)
                    saved = add("SaveImage", images=result, filename_prefix="creative-upscale")
                if size:
                    node_dimensions[key] = output_dimensions(*size, int(scale[0])) if output_types[reference_ids[0]] == 'video' else tuple(value * int(scale[0]) for value in size)
            output_types[key] = "video" if kind == "video" or (kind == "upscale" and output_types[reference_ids[0]] == "video") else "image"
            outputs[saved[0]] = key
        else:
            raise ValueError("这个节点不支持直接生成")
        visiting.remove(key)
        resolved[key] = result
        return result

    for target in targets:
        build(target, force=True)
    return {"graph": graph, "asset_ids": assets, "asset_media_types": asset_media_types, "models": sorted(models),
            "node_models": node_models, "output_nodes": outputs,
            'reference_plans': reference_plans, 'node_dimensions': node_dimensions, 'node_seeds': node_seeds,
            "postprocess": postprocess, "postprocess_nodes": postprocess_nodes, "target_node": targets[0], "target_nodes": targets}
