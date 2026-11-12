import io

import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image

from backend.app import create_app
from tests.fake_colab import PROFILES, FakeColab


@pytest.fixture
async def client(tmp_path):
    app = create_app(tmp_path, driver=FakeColab(), account_profiles=PROFILES)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            client.headers["x-workbench-token"] = (await client.get("/api/state")).json()["token"]
            yield client


async def test_project_roundtrip_and_stale_save_cannot_overwrite(client):
    response = await client.post("/api/projects", json={"name": "测试画布"})
    assert response.status_code == 201
    project = response.json()
    canvas = project["canvases"][0]
    canvas["nodes"] = [{"id": "image1", "type": "creative", "position": {"x": 120, "y": 220},
                        "data": {"kind": "image", "label": "照片", "prompt": "蓝天"}}]
    response = await client.put(f"/api/projects/{project['id']}", json=project)
    assert response.status_code == 200
    assert response.json()["revision"] == 1
    assert (await client.put(f"/api/projects/{project['id']}", json=project)).status_code == 409
    restored = (await client.get(f"/api/projects/{project['id']}")).json()
    assert restored["canvases"][0]["nodes"][0]["data"]["prompt"] == "蓝天"
    assert restored["canvases"][0]["nodes"][0]["position"] == {"x": 120, "y": 220}


async def test_folders_cannot_form_cycles_and_trash_is_recoverable(client):
    folder = (await client.post("/api/projects", json={"name": "素材", "kind": "folder"})).json()
    child = (await client.post("/api/projects", json={"name": "子文件夹", "kind": "folder", "folder_id": folder["id"]})).json()
    assert (await client.patch(f"/api/projects/{folder['id']}", json={"folder_id": child["id"]})).status_code == 400
    assert (await client.patch(f"/api/projects/{folder['id']}", json={"deleted": True})).status_code == 200
    items = (await client.get("/api/projects")).json()
    assert next(x for x in items if x["id"] == folder["id"])["deleted_at"]
    await client.patch(f"/api/projects/{folder['id']}", json={"deleted": False})
    assert not (await client.get(f"/api/projects/{folder['id']}")).json()["deleted_at"]


async def test_invalid_graph_never_replaces_saved_canvas(client):
    project = (await client.post("/api/projects", json={"name": "图"})).json()
    project["canvases"][0]["edges"] = [{"id": "e", "source": "missing", "target": "missing2"}]
    assert (await client.put(f"/api/projects/{project['id']}", json=project)).status_code == 400
    restored = (await client.get(f"/api/projects/{project['id']}")).json()
    assert restored["canvases"][0]["edges"] == []


@pytest.mark.parametrize("field,value", [("nodes", [None]), ("edges", [None])])
async def test_malformed_canvas_payload_is_a_validation_error(client, field, value):
    project = (await client.post("/api/projects", json={"name": "验证"})).json()
    project["canvases"][0][field] = value
    response = await client.put(f"/api/projects/{project['id']}", json=project)
    assert response.status_code == 400


async def test_duplicate_is_independent_and_folder_move_validated(client):
    project = (await client.post("/api/projects", json={"name": "原稿"})).json()
    copy = await client.post(f"/api/projects/{project['id']}/duplicate")
    assert copy.status_code == 201
    assert copy.json()["id"] != project["id"]
    assert copy.json()["canvases"][0]["id"] != project["canvases"][0]["id"]
    assert (await client.patch(f"/api/projects/{copy.json()['id']}", json={"folder_id": project["id"]})).status_code == 400


async def test_asset_checks_content_and_preserves_bytes(client):
    image = io.BytesIO()
    Image.new("RGB", (8, 8), "blue").save(image, format="PNG")
    content = image.getvalue()
    response = await client.post("/api/assets", content=content, headers={"x-filename": "picture.png"})
    assert response.status_code == 201
    asset = response.json()
    assert asset["media_type"] == "image/png"
    assert (await client.get(f"/api/assets/{asset['id']}/file")).content == content
    assert (await client.post("/api/assets", content=b"<script>oops</script>", headers={"x-filename": "fake.png"})).status_code == 400
    assert (await client.get("/api/assets/missing/file")).status_code == 404
