import pytest

from backend.creative_workflows import compile_canvas
from backend.workflows import validate_graph


def node(key, kind, **data):
    return {"id": key, "type": "creative", "position": {"x": 0, "y": 0},
            "data": {"kind": kind, "label": key, "prompt": "A forest", **data}}


def test_image_to_video_compiles_as_one_graph_and_loop_uses_same_frame():
    canvas = {"nodes": [node("photo", "image", model="minimax-image"),
                         node("movie", "video", prompt="以@photo 为首帧，首尾循环", duration=10, fps=30,
                              mode="首尾循环", aspect="16:9", resolution="768p", audio=False)],
              "edges": [{"source": "photo", "target": "movie"}]}
    result = compile_canvas(canvas, "movie")
    validate_graph(result["graph"])
    graph = result["graph"]
    video = next(n["inputs"] for n in graph.values() if n["class_type"] == "MiniMaxH3ImageToVideo")
    assert video["first_frame"] == video["last_frame"]
    assert video["length"] == 243
    assert video["width"] * video["height"] <= 768 * 1344
    assert {"photo", "movie"} == set(result["output_nodes"].values())
    assert result["postprocess"]["fps"] == 30
    assert next(n["inputs"] for n in graph.values() if n["class_type"] == "CreateVideo")["fps"] == 24
    assert not any(n["class_type"] == "VAEDecodeAudio" for n in graph.values())


def test_moss_reference_uses_edit_conditioning_and_lora():
    canvas = {"nodes": [node("reference", "upload", asset_id="abc"), node("image", "image", model="moss-image")],
              "edges": [{"source": "reference", "target": "image"}]}
    result = compile_canvas(canvas, "image")
    validate_graph(result["graph"])
    assert result["asset_ids"] == ["abc"]
    types = {n["class_type"] for n in result["graph"].values()}
    assert {"LoraLoaderModelOnly", "TextEncodeMOSSImageEdit"} <= types
    assert "moss-edit" in result["models"]
    encoders = [n['inputs'] for n in result['graph'].values() if n['class_type'] == 'TextEncodeMOSSImageEdit']
    assert all('images.image_0' in n for n in encoders)


def test_rejects_unsupported_sizes_and_missing_upscale_input_before_allocation():
    for n in [node("v", "video", resolution="4K"), node("v", "upscale")]:
        with pytest.raises(ValueError):
            compile_canvas({"nodes": [n], "edges": []}, "v")


def test_text_reference_is_inlined_and_does_not_require_media():
    canvas = {"nodes": [node("script", "text", prompt="A red fox in snow"),
                         node("image", "image", prompt="根据@script 绘制", model="minimax-image")],
              "edges": [{"source": "script", "target": "image"}]}
    result = compile_canvas(canvas, "image")
    assert result["asset_ids"] == []
    conditioning = next(n["inputs"] for n in result["graph"].values() if n["class_type"] == "MiniMaxH3ImageConditioning")
    assert "A red fox in snow" in conditioning["prompt"]


def test_cycle_is_rejected_without_recursing_forever():
    canvas = {"nodes": [node("a", "image"), node("b", "image")],
              "edges": [{"source": "a", "target": "b"}, {"source": "b", "target": "a"}]}
    with pytest.raises(ValueError, match="循环"):
        compile_canvas(canvas, "a")


def test_downstream_uses_selected_version_asset_and_regeneration_ignores_own_old_asset():
    canvas = {"nodes": [node("image", "image", asset_id="chosen-old", active_version_id="v1",
                             versions=[{"id": "v1", "asset_id": "chosen-old"}, {"id": "v2", "asset_id": "newer"}]),
                         node("movie", "video", duration=5)],
              "edges": [{"source": "image", "target": "movie"}]}
    assert compile_canvas(canvas, "movie")["asset_ids"] == ["chosen-old"]
    regenerated = compile_canvas(canvas, "image")
    assert regenerated["asset_ids"] == []
    assert "image" in regenerated["output_nodes"].values()


def test_batch_compiles_shared_upstream_once_and_uses_dependency_order():
    canvas = {"nodes": [node("image", "image"), node("a", "upscale", resolution="2×"), node("b", "upscale", resolution="4×")],
              "edges": [{"source": "image", "target": "a"}, {"source": "image", "target": "b"}]}
    result = compile_canvas(canvas, ["a", "image", "b"])
    validate_graph(result['graph'])
    assert sum(n['class_type'] == 'KSampler' for n in result['graph'].values()) == 1
    assert set(result['output_nodes'].values()) == {'image', 'a', 'b'}
