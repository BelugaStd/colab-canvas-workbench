"""Load the original MiniMax Image shards without quantization or AdaLN pruning."""
import json
import os
import re
import signal
import subprocess
import time
import urllib.request
from pathlib import Path
from types import MethodType

ROOT = Path('/content/colab-workbench')
REPOSITORY = 'MiniMaxAI/MiniMax-H3-Image'
REVISION = '5f90473023975e6271b0ed1dd84c4e88e4bbc6d3'
FILES = [f'transformer/model-{i:05d}-of-00012.safetensors' for i in range(1, 13)] + [
    'transformer/model.safetensors.index.json', 'pdd_fused_heads.safetensors', 'pdd_config.json',
]
NODE = 'WorkbenchMiniMaxImageFullLoader'
RUNTIME_DESCRIPTION = 'Official full BF16 loader; workbench memory policy v1'


def runtime_ready(info):
    return info.get(NODE, {}).get('description') == RUNTIME_DESCRIPTION


def load_full_state(directory, read_tensors):
    index = json.loads((directory / 'transformer/model.safetensors.index.json').read_text())['weight_map']
    if not index or any(not re.fullmatch(r'model-\d{5}-of-00012\.safetensors', name) for name in index.values()):
        raise ValueError('MiniMax 官方权重索引无效')
    state = {}
    qkv_rows = [head * 384 + part * 128 + channel
                for part in range(3) for head in range(56) for channel in range(128)]
    for filename in sorted(set(index.values())):
        shard = read_tensors(str(directory / 'transformer' / filename))
        expected = {key for key, name in index.items() if name == filename}
        if set(shard) != expected or state.keys() & shard.keys():
            raise ValueError('MiniMax 官方权重分片缺失、重复或与索引不匹配')
        for key, value in shard.items():
            if key.endswith('.attn.qkv_proj.weight'):
                if tuple(value.shape) != (21504, 5376):
                    raise ValueError('MiniMax 官方注意力权重形状不匹配')
                # WARN: Raw shards interleave Q/K/V per head; ComfyUI expects all Q, then K, then V.
                shard[key] = value[qkv_rows]
        state.update(shard)
    if ('time_embedder.proj_in.weight' not in state or 'adaln_t_table' in state
            or any(tuple(getattr(state.get(f'blocks.{i}.adaln_proj.linear.weight'), 'shape', ())) != (96768, 2688)
                   for i in range(50))):
        raise ValueError('需要 MiniMax 官方 50 层未裁剪全量权重')
    config = json.loads((directory / 'pdd_config.json').read_text())
    sigmas = config['sigmas']
    if (config['num_inference_steps'] != 6 or len(sigmas) != 7 or sigmas[0] != 1 or sigmas[-1] != 0
            or not all(a > b for a, b in zip(sigmas, sigmas[1:]))):
        raise ValueError('MiniMax 官方六步采样配置无效')
    heads = read_tensors(str(directory / 'pdd_fused_heads.safetensors'))
    if tuple(heads['video_out.weight'].shape) != (6, 96, 5376) or tuple(heads['video_out.bias'].shape) != (6, 96):
        raise ValueError('MiniMax 官方 PDD 输出头不匹配')
    # WARN: ComfyUI adds head zero to later rows; official full heads must become offsets first.
    for name, shape in [('weight', (576, 5376)), ('bias', (576,))]:
        stacked = heads['video_out.' + name].clone()
        stacked[1:] = stacked[1:] - stacked[:1]
        state['final_layer.video_out.' + name] = stacked.reshape(*shape)
    return state, {'config': json.dumps({'transformer': {'pdd_steps': 6, 'pdd_sigmas': sigmas}})}


def full_time_embedding(self, unique_t, device, dtype):
    import torch

    return self.time_embedder(torch.tensor(unique_t, dtype=torch.float32, device=device))


def full_adaln_forward(self, embedding):
    import torch

    # WARN: The official FP32 time embedding must pass SiLU before the BF16 projection cast.
    activated = torch.nn.functional.silu(embedding).to(self.linear.weight.dtype)
    projected = self.linear(activated).view(embedding.shape[0] * self.modalities, self.expand * self.hidden)
    return projected.chunk(self.expand, dim=-1)


def load_model():
    import comfy.sd
    import comfy.utils
    import torch

    state, metadata = load_full_state(ROOT / 'ComfyUI/models/minimax-image-full', comfy.utils.load_torch_file)
    model = comfy.sd.load_diffusion_model_state_dict(state, model_options={'dtype': torch.bfloat16}, metadata=metadata)
    if model is None:
        raise RuntimeError('ComfyUI 无法加载官方 MiniMax Image 全量 BF16 模型')
    transformer = model.model.diffusion_model
    transformer._time_embedding = MethodType(full_time_embedding, transformer)
    for layer in [*transformer.blocks, transformer.final_layer]:
        layer.adaln_proj.forward = MethodType(full_adaln_forward, layer.adaln_proj)
    model.cached_patcher_init = (load_model, ())
    return model


class MiniMaxImageFullLoader:
    DESCRIPTION = RUNTIME_DESCRIPTION

    @classmethod
    def INPUT_TYPES(cls):
        return {'required': {}}

    RETURN_TYPES = ('MODEL',)
    FUNCTION = 'load'
    CATEGORY = 'workbench'

    def load(self):
        return (load_model(),)


NODE_CLASS_MAPPINGS = {NODE: MiniMaxImageFullLoader}
NODE_DISPLAY_NAME_MAPPINGS = {NODE: 'MiniMax Image · Official Full BF16'}


def remote_json(path):
    with urllib.request.urlopen('http://127.0.0.1:8188' + path, timeout=10) as response:
        return json.load(response)


def ensure_runtime():
    if runtime_ready(remote_json('/object_info/' + NODE)):
        return
    queue = remote_json('/queue')
    # WARN: Installing this loader must never interrupt an existing ComfyUI job.
    if queue.get('queue_running') or queue.get('queue_pending'):
        raise RuntimeError('ComfyUI 仍有任务，请等待完成后再启用全量图片模型')
    pid = int((ROOT / 'server.pid').read_text())
    command = (Path('/proc') / str(pid) / 'cmdline').read_bytes().split(b'\0')
    if str(ROOT / 'ComfyUI/main.py').encode() not in command:
        raise RuntimeError('无法确认 ComfyUI 进程，拒绝重启其它服务')
    os.kill(pid, signal.SIGTERM)
    for _ in range(200):
        try:
            stat = (Path('/proc') / str(pid) / 'stat').read_text()
        except FileNotFoundError:
            break
        if stat.split(') ', 1)[1].startswith('Z '):
            break
        time.sleep(.1)
    else:
        raise TimeoutError('等待原 ComfyUI 正常退出超时')
    subprocess.run([str(ROOT / 'venv/bin/python'), str(ROOT / 'deploy_remote.py'), '--serve'], check=True, timeout=180)
    if not runtime_ready(remote_json('/object_info/' + NODE)):
        raise RuntimeError('全量图片加载器未就绪，请检查 ComfyUI 日志')


if __name__ == '__main__':
    ensure_runtime()
