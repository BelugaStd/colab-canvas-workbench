"""Local creative projects. Canvas records never contain executable workflow code."""
import copy
import json
import math
import time
import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NewProject(StrictModel):
    name: str = Field(default="Untitled", min_length=1, max_length=100)
    kind: Literal["project", "folder"] = "project"
    folder_id: str | None = None


class ProjectChange(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    folder_id: str | None = None
    deleted: bool | None = None


class ProjectSave(StrictModel):
    id: str
    name: str = Field(min_length=1, max_length=100)
    kind: Literal["project", "folder"]
    folder_id: str | None = None
    canvases: list[dict] = Field(max_length=50)
    revision: int = Field(ge=0)
    created_at: float
    updated_at: float
    deleted_at: float | None = None


class Generate(StrictModel):
    account: str
    stop_after: bool = False


class GenerateBatch(Generate):
    nodes: list[str] = Field(min_length=1, max_length=20)


def validate_canvases(canvases):
    if len(json.dumps(canvases, allow_nan=False)) > 4_000_000:
        raise ValueError("项目超过 4 MB，请拆分画布")
    canvas_ids = set()
    for canvas in canvases:
        if not isinstance(canvas, dict):
            raise ValueError("画布格式无效")
        key = canvas.get("id")
        if not isinstance(key, str) or not key or key in canvas_ids:
            raise ValueError("画布 ID 重复或无效")
        canvas_ids.add(key)
        if not isinstance(canvas.get("name"), str) or not 1 <= len(canvas["name"]) <= 100:
            raise ValueError("画布名称须为 1–100 个字符")
        nodes, edges = canvas.get("nodes"), canvas.get("edges")
        if not isinstance(nodes, list) or len(nodes) > 500 or not isinstance(edges, list) or len(edges) > 2000:
            raise ValueError("每张画布最多 500 个节点和 2000 条连线")
        ids = set()
        for node in nodes:
            if not isinstance(node, dict):
                raise ValueError("节点格式无效")
            key, data, pos = node.get("id"), node.get("data", {}), node.get("position", {})
            if not isinstance(data, dict) or not isinstance(pos, dict):
                raise ValueError("节点数据或位置无效")
            if not isinstance(key, str) or not key or key in ids or node.get("type") != "creative":
                raise ValueError("节点 ID 重复或类型无效")
            ids.add(key)
            if data.get("kind") not in {"image", "video", "upload", "upscale", "text", "audio", "group"}:
                raise ValueError("未知的创意节点类型")
            if not isinstance(data.get("label"), str) or len(data["label"]) > 100:
                raise ValueError("节点名称无效")
            if not isinstance(data.get("prompt", ""), str) or len(data.get("prompt", "")) > 20000:
                raise ValueError("提示词最多 20000 个字符")
            for axis in ("x", "y"):
                value = pos.get(axis)
                if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > 1_000_000:
                    raise ValueError("节点位置无效")
        deps, edge_ids = {key: [] for key in ids}, set()
        for node in nodes:
            parent = node.get("parentId")
            if parent is not None:
                group = next((n for n in nodes if n["id"] == parent), None)
                if not group or group["data"]["kind"] != "group" or node["data"]["kind"] == "group":
                    raise ValueError("分组引用无效；分组不能嵌套")
            if node["data"]["kind"] == "group":
                style = node.get("style", {})
                if not isinstance(style, dict) or any(type(style.get(axis)) not in (int, float) or not 100 <= style[axis] <= 100000 for axis in ("width", "height")):
                    raise ValueError("分组尺寸无效")
        for edge in edges:
            if not isinstance(edge, dict):
                raise ValueError("连线格式无效")
            key, source, target = edge.get("id"), edge.get("source"), edge.get("target")
            if not isinstance(key, str) or key in edge_ids or source not in ids or target not in ids:
                raise ValueError("连线引用了不存在的节点，或 ID 重复")
            edge_ids.add(key)
            deps[target].append(source)
        visited, active = set(), set()

        def visit(key):
            if key in active:
                raise ValueError("不能创建循环参考")
            if key in visited:
                return
            active.add(key)
            for dep in deps[key]:
                visit(dep)
            active.remove(key)
            visited.add(key)

        for key in ids:
            visit(key)


router = APIRouter(prefix="/api/projects")


def store(request):
    return request.app.state.store


def get_project(request, key):
    item = store(request).get("projects", key)
    if not item:
        raise HTTPException(404, "项目不存在")
    return item


def validate_folder(request, folder_id, key=None):
    seen = {key} if key else set()
    while folder_id:
        if folder_id in seen:
            raise ValueError("文件夹不能移动到自身或子文件夹")
        seen.add(folder_id)
        folder = get_project(request, folder_id)
        if folder["kind"] != "folder" or folder.get("deleted_at"):
            raise ValueError("目标必须是可用的文件夹")
        folder_id = folder.get("folder_id")


@router.get("")
async def list_projects(request: Request):
    result = []
    for item in store(request).all("projects"):
        summary = {k: v for k, v in item.items() if k != "canvases"}
        summary["canvas_count"] = len(item.get("canvases", []))
        summary["thumbnail_asset"] = next((n["data"]["asset_id"] for c in item.get("canvases", [])
                                            for n in c["nodes"] if n["data"].get("asset_id")), None)
        summary["thumbnail_type"] = store(request).get("assets", summary["thumbnail_asset"], {}).get("media_type", "image/png")
        result.append(summary)
    return sorted(result, key=lambda p: p["updated_at"], reverse=True)


@router.post("", status_code=201)
async def create_project(body: NewProject, request: Request):
    validate_folder(request, body.folder_id)
    now, key = time.time(), uuid.uuid4().hex
    item = {"id": key, **body.model_dump(), "revision": 0, "created_at": now, "updated_at": now,
            "deleted_at": None, "canvases": []}
    if body.kind == "project":
        item["canvases"].append({"id": uuid.uuid4().hex, "name": "Untitled", "nodes": [], "edges": [],
                                 "viewport": {"x": 0, "y": 0, "zoom": 0.7142857}})
    return store(request).put("projects", key, item)


@router.get("/{key}")
async def read_project(key: str, request: Request):
    return get_project(request, key)


@router.post("/{key}/canvases/{canvas_id}/nodes/{node_id}/generate", status_code=202)
async def generate_node(key: str, canvas_id: str, node_id: str, body: Generate, request: Request):
    return submit_canvas(key, canvas_id, [node_id], body, request)


@router.post("/{key}/canvases/{canvas_id}/generate", status_code=202)
async def generate_batch(key: str, canvas_id: str, body: GenerateBatch, request: Request):
    return submit_canvas(key, canvas_id, body.nodes, body, request)


def submit_canvas(key, canvas_id, node_ids, body, request):
    from .assets import asset_path
    from .creative_workflows import compile_canvas
    from .manager import ACTIVE

    project = get_project(request, key)
    if project.get("deleted_at"):
        raise ValueError("请先恢复项目再生成")
    canvas = next((c for c in project["canvases"] if c["id"] == canvas_id), None)
    if not canvas:
        raise HTTPException(404, "画布不存在")
    manager = request.app.state.manager
    if not any(p["id"] == body.account and p.get("credential_present") for p in manager.profiles):
        raise ValueError("请先在算力面板登录 Colab 账号")
    for job in store(request).all("jobs"):
        run = job.get("creative", {})
        if job["status"] in ACTIVE and run.get("project_id") == key and run.get("canvas_id") == canvas_id and set(node_ids).intersection(run.get("output_nodes", {}).values()):
            raise HTTPException(409, "这个节点已有生成任务，请等待完成或先取消")
    compiled = compile_canvas(canvas, node_ids)
    for job in store(request).all("jobs"):
        run = job.get("creative", {})
        if job["status"] in ACTIVE and run.get("project_id") == key and run.get("canvas_id") == canvas_id and set(compiled["output_nodes"].values()).intersection(run.get("output_nodes", {}).values()):
            raise HTTPException(409, "上游参考正在生成，请等待完成后再生成下游")
    for asset_id in compiled["asset_ids"]:
        _, asset = asset_path(store(request), asset_id)
        if not asset["media_type"].startswith("image/"):
            raise ValueError("请连接图片素材作为参考")
    target = next(n for n in canvas["nodes"] if n["id"] == node_ids[0])
    metadata = {k: v for k, v in compiled.items() if k != "graph"}
    metadata.update(project_id=key, canvas_id=canvas_id)
    settings_keys = {"prompt", "model", "aspect", "resolution", "quality", "duration", "fps", "audio", "mode"}
    metadata["node_settings"] = {n["id"]: copy.deepcopy({k: v for k, v in n["data"].items() if k in settings_keys})
                                 for n in canvas["nodes"] if n["id"] in compiled["output_nodes"].values()}
    name = target["data"]["label"] if len(node_ids) == 1 else f"批量生成 · {len(node_ids)} 个节点"
    return manager.submit(body.account, compiled["graph"], name, body.stop_after, creative=metadata)


@router.put("/{key}")
async def save_project(key: str, body: ProjectSave, request: Request):
    old = get_project(request, key)
    if body.id != key or body.kind != old["kind"]:
        raise ValueError("项目 ID 或类型不能更改")
    if old["revision"] != body.revision:
        raise HTTPException(409, "项目已在另一个窗口更新，请重新打开后再编辑")
    validate_folder(request, body.folder_id, key)
    validate_canvases(body.canvases)
    if body.kind == "folder" and body.canvases:
        raise ValueError("文件夹不能包含画布")
    if body.kind == "project" and not body.canvases:
        raise ValueError("项目至少保留一张画布")
    item = {**body.model_dump(), "created_at": old["created_at"], "deleted_at": old.get("deleted_at"),
            "updated_at": time.time(), "revision": old["revision"] + 1}
    return store(request).put("projects", key, item)


@router.patch("/{key}")
async def change_project(key: str, body: ProjectChange, request: Request):
    item = get_project(request, key)
    if "folder_id" in body.model_fields_set:
        validate_folder(request, body.folder_id, key)
        item["folder_id"] = body.folder_id
    if body.name is not None:
        item["name"] = body.name
    if body.deleted is not None:
        item["deleted_at"] = time.time() if body.deleted else None
    item.update(updated_at=time.time(), revision=item["revision"] + 1)
    return store(request).put("projects", key, item)


@router.post("/{key}/duplicate", status_code=201)
async def duplicate_project(key: str, request: Request):
    item = copy.deepcopy(get_project(request, key))
    if item["kind"] != "project":
        raise ValueError("请在文件夹内复制具体项目")
    item.update(id=uuid.uuid4().hex, name=(item["name"][:95] + " 副本"), revision=0,
                created_at=time.time(), updated_at=time.time(), deleted_at=None)
    for canvas in item["canvases"]:
        canvas["id"] = uuid.uuid4().hex
    return store(request).put("projects", item["id"], item)
