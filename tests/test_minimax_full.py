import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend import deploy_remote, minimax_full_remote
from backend.creative_workflows import compile_canvas
from tests.test_creative_workflows import node


class Tensor:
    def __init__(self, *shape, dtype='BF16', values=None):
        self.shape, self.dtype = shape, dtype
        self.values = values

    def reshape(self, *shape):
        return Tensor(*shape, dtype=self.dtype, values=self.values)

    def clone(self):
        return Tensor(*self.shape, dtype=self.dtype, values=self.values.copy())

    def __getitem__(self, index):
        values = self.values[index]
        return Tensor(len(values), *self.shape[1:], dtype=self.dtype, values=values)

    def __sub__(self, other):
        return Tensor(*self.shape, dtype=self.dtype, values=[v - other.values[0] for v in self.values])

    def __setitem__(self, index, value):
        self.values[index] = value.values

    def to(self, dtype):
        return Tensor(*self.shape, dtype=dtype, values=self.values)

    view = reshape

    def chunk(self, count, dim):
        return [Tensor(self.shape[0], self.shape[1] // count, dtype=self.dtype)] * count


@pytest.mark.parametrize('prefix', ['blocks.0', 'token_refiner.blocks.1'])
def test_raw_qkv_is_deinterleaved_without_changing_values_or_precision(tmp_path, prefix):
    tensors, read = source_fixture(tmp_path)
    class Rows:
        shape = (56 * 3 * 128, 5376)
        dtype = 'BF16'
        def __getitem__(self, indices):
            return SimpleNamespace(values=indices, shape=self.shape, dtype=self.dtype)
    key = prefix + '.attn.qkv_proj.weight'
    tensors[key] = Rows()
    name = 'model-00001-of-00012.safetensors'
    (tmp_path / 'transformer/model.safetensors.index.json').write_text(json.dumps({'weight_map': dict.fromkeys(tensors, name)}))
    state, _ = minimax_full_remote.load_full_state(tmp_path, read)
    rows = state[key].values
    assert rows[:3] == [0, 1, 2]
    assert rows[127:130] == [127, 384, 385]
    assert rows[7168:7171] == [128, 129, 130]
    assert rows[14336:14339] == [256, 257, 258]
    assert rows[-1] == 21503
    assert sorted(rows) == list(range(21504))
    assert state[key].dtype == 'BF16'
    assert state[key].shape == Rows.shape


def source_fixture(tmp_path):
    tensors = {'time_embedder.proj_in.weight': Tensor(5376, 256, dtype='F32')}
    tensors.update({f'blocks.{i}.adaln_proj.linear.weight': Tensor(96768, 2688) for i in range(50)})
    name = 'model-00001-of-00012.safetensors'
    (tmp_path / 'transformer').mkdir()
    (tmp_path / 'transformer/model.safetensors.index.json').write_text(json.dumps({'weight_map': dict.fromkeys(tensors, name)}))
    (tmp_path / 'pdd_config.json').write_text(json.dumps({'num_inference_steps': 6, 'sigmas': [1, .96, .88, .75, .53, .23, 0]}))
    heads = {'video_out.weight': Tensor(6, 96, 5376, dtype='F32', values=[10, 15, 3, 12, -4, 18]),
             'video_out.bias': Tensor(6, 96, dtype='F32', values=[1, -2, 4, 3, 5, 8])}
    def read(path):
        path.lower()
        return heads if Path(path).name == 'pdd_fused_heads.safetensors' else tensors.copy()
    return tensors, read


def test_full_original_shards_preserve_tensors_and_use_official_six_step_heads(tmp_path):
    original, read = source_fixture(tmp_path)
    state, metadata = minimax_full_remote.load_full_state(tmp_path, read)
    assert all(state[k] is value for k, value in original.items())
    assert state['final_layer.video_out.weight'].shape == (576, 5376)
    assert state['final_layer.video_out.bias'].shape == (576,)
    assert state['final_layer.video_out.weight'].dtype == 'F32'
    weights = state['final_layer.video_out.weight'].values
    biases = state['final_layer.video_out.bias'].values
    assert [weights[0], *[weights[0] + v for v in weights[1:]]] == [10, 15, 3, 12, -4, 18]
    assert [biases[0], *[biases[0] + v for v in biases[1:]]] == [1, -2, 4, 3, 5, 8]
    assert json.loads(metadata['config'])['transformer']['pdd_steps'] == 6
    assert 'adaln_t_table' not in state


@pytest.mark.parametrize('problem', ['missing', 'pruned', 'path_escape', 'wrong_heads'])
def test_full_loader_rejects_incomplete_or_altered_originals(tmp_path, problem):
    tensors, read = source_fixture(tmp_path)
    if problem == 'missing':
        del tensors['blocks.49.adaln_proj.linear.weight']
    elif problem == 'pruned':
        tensors['blocks.0.adaln_proj.linear.weight'] = Tensor(96768, 8, dtype='F16')
    elif problem == 'path_escape':
        (tmp_path / 'transformer/model.safetensors.index.json').write_text(json.dumps({'weight_map': {'x': '../../outside.safetensors'}}))
    else:
        original_read = read
        def read(path):
            return {'video_out.weight': Tensor(1, 96, 5376), 'video_out.bias': Tensor(6, 96)} if Path(path).name == 'pdd_fused_heads.safetensors' else original_read(path)
    with pytest.raises(ValueError):
        minimax_full_remote.load_full_state(tmp_path, read)


@pytest.mark.parametrize('edit', [False, True])
def test_minimax_images_use_full_dit_and_nonquantized_text_encoder(edit):
    nodes = [node('image', 'image', model='minimax-image', resolution='4K', image_mode='edit' if edit else 'text')]
    edges = []
    if edit:
        nodes.append(node('ref', 'upload', asset_id='source'))
        edges.append({'source': 'ref', 'target': 'image'})
    compiled = compile_canvas({'nodes': nodes, 'edges': edges}, 'image')
    assert compiled['models'] == ['minimax-image-full']
    assert any(n['class_type'] == 'WorkbenchMiniMaxImageFullLoader' for n in compiled['graph'].values())
    encoder = next(n for n in compiled['graph'].values() if n['class_type'] == 'CLIPLoader')
    assert encoder['inputs']['clip_name'] == 'qwen3vl_32b_minimax_h3_bf16.safetensors'
    assert not any('int8' in str(n) or 'nvfp4' in str(n) or 'pruned' in str(n) for n in compiled['graph'].values())


def test_full_preset_downloads_pinned_original_shards_with_a_separate_destination():
    files = deploy_remote.MODEL_GROUPS['minimax-image-full']
    original = [(repo, revision, name) for repo, revision, name in files if repo == 'MiniMaxAI/MiniMax-H3-Image']
    assert len(original) == 15
    assert all(revision == '5f90473023975e6271b0ed1dd84c4e88e4bbc6d3' for _, revision, _ in original)
    assert sum(name.endswith('.safetensors') and name.startswith('transformer/') for _, _, name in original) == 12
    assert any(name.endswith('_bf16.safetensors') for _, _, name in files)
    assert deploy_remote.model_destination(original[0][0], original[0][2]).startswith('minimax-image-full/')


@pytest.mark.parametrize('modalities,expand', [(3, 6), (1, 2)])
def test_official_fp32_time_embedding_is_activated_before_bf16_projection(monkeypatch, modalities, expand):
    activated, projected = [], []
    def silu(value):
        activated.append(value.dtype)
        return value
    fake_torch = SimpleNamespace(float32='F32', tensor=lambda values, **kw: Tensor(len(values), dtype=kw['dtype']),
                                 nn=SimpleNamespace(functional=SimpleNamespace(silu=silu)))
    monkeypatch.setitem(sys.modules, 'torch', fake_torch)
    model = SimpleNamespace(time_embedder=lambda t: Tensor(2, 2688, dtype=t.dtype))
    embedding = minimax_full_remote.full_time_embedding(model, [0., 1.], 'cpu', 'BF16')
    class Linear:
        weight = SimpleNamespace(dtype='BF16')
        def __call__(self, value):
            projected.append(value.dtype)
            return Tensor(2, modalities * expand * 5376, dtype=value.dtype)
    layer = SimpleNamespace(linear=Linear(), modalities=modalities, expand=expand, hidden=5376)
    result = minimax_full_remote.full_adaln_forward(layer, embedding)
    assert embedding.dtype == 'F32'
    assert activated == ['F32']
    assert projected == ['BF16']
    assert len(result) == expand
    assert result[0].shape == (2 * modalities, 5376)


def test_runtime_upgrade_refuses_to_restart_a_busy_comfy(monkeypatch, tmp_path):
    monkeypatch.setattr(minimax_full_remote, 'ROOT', tmp_path)
    monkeypatch.setattr(minimax_full_remote, 'remote_json', lambda path: {} if path.startswith('/object_info') else {'queue_running': [[1, 'user-job']], 'queue_pending': []})
    calls = []
    monkeypatch.setattr(minimax_full_remote, 'subprocess', SimpleNamespace(run=lambda *a, **kw: calls.append(a)))
    with pytest.raises(RuntimeError, match='任务'):
        minimax_full_remote.ensure_runtime()
    assert not calls


async def test_existing_gpu_installs_missing_loader_once_and_reuses_it(tmp_path, monkeypatch):
    from backend.manager import Manager
    from backend.store import Store
    from tests.fake_colab import PROFILES, FakeColab

    fake, store = FakeColab(), Store(tmp_path)
    manager = Manager(store, fake, PROFILES)
    await manager.open()
    try:
        await manager.start('colab2')
        original = manager.machine('colab2').copy()
        fake.full_loader_ready = False
        installs = []
        async def execute(account, session, script):
            assert session == original['session']
            compile(script.read_text(), str(script), 'exec')
            assert 'minimax_full_remote.py' in script.read_text()
            installs.append(session)
            await asyncio.sleep(.05)
            fake.full_loader_ready = True
            return 'ready'
        monkeypatch.setattr(fake, 'execute', execute)
        await asyncio.gather(manager.ensure_image_runtime('colab2'), manager.ensure_image_runtime('colab2'))
        await manager.ensure_image_runtime('colab2')
        assert installs == [original['session']]
        assert manager.machine('colab2')['session'] == original['session']
        assert sum(c[1] == 'create' for c in fake.calls) == 1
        assert not fake.prompt_posts
    finally:
        await manager.close()
        store.close()
