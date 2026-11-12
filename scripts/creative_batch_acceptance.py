"""Explicit paid batch acceptance against the isolated validation service."""
import asyncio
import json
import time
from pathlib import Path

import aiohttp

from backend.creative_workflows import compile_canvas

BASE = "http://127.0.0.1:8777/api"
LEDGER = Path(__file__).resolve().parents[2] / "creative-validation/cu-ledger.json"


async def main():
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as http:
        async with http.get(BASE + "/state") as response:
            token = (await response.json())["token"]

        async def call(path, body=None, method=None):
            async with http.request(method or ("GET" if body is None else "POST"), BASE + path,
                                    json=body, headers={"X-Workbench-Token": token}) as response:
                result = await response.json()
                if response.status >= 400:
                    raise RuntimeError(result)
                return result

        for _ in range(120):
            state = await call("/state")
            machine = state["accounts"][0]["machine"]
            if machine["phase"] == "ready":
                break
            if machine["phase"] in {"error", "stopped"}:
                raise RuntimeError(machine["message"])
            await asyncio.sleep(3)
        else:
            raise TimeoutError("Connection did not become ready")
        ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
        usage = await call("/accounts/colab1/refresh", {})
        if ledger["baseline"]["usage"]["balance"] - usage["usage"]["balance"] >= ledger["limit_cu"] - 2:
            raise RuntimeError("CU budget reached")
        project = await call("/projects", {"name": "批量实机验收 · 共享上游"})
        canvas = project["canvases"][0]
        canvas["nodes"] = [
            {"id": "source", "type": "creative", "position": {"x": 80, "y": 180}, "data": {
                "kind": "image", "label": "共享图片", "model": "minimax-image", "resolution": "1K",
                "aspect": "1:1", "quality": "标准", "seed": 12345,
                "prompt": "A small red fox beside a still alpine lake, soft dawn light, painterly illustration, no text."}},
            *[{"id": key, "type": "creative", "position": {"x": 620, "y": y}, "data": {
                "kind": "upscale", "label": label, "resolution": scale}}
              for key, label, scale, y in [("up2", "超分 2×", "2×", 60), ("up4", "超分 4×", "4×", 470)]]]
        canvas["edges"] = [{"id": key, "source": "source", "target": key} for key in ("up2", "up4")]
        canvas["viewport"] = {"x": 30, "y": 20, "zoom": 0.6}
        compiled = compile_canvas(canvas, ["up2", "up4"])
        assert sum(n["class_type"] == "KSampler" for n in compiled["graph"].values()) == 1
        project = await call("/projects/" + project["id"], project, "PUT")
        job = await call(f"/projects/{project['id']}/canvases/{canvas['id']}/generate",
                         {"account": "colab1", "nodes": ["up2", "up4"]})
        ledger["batch_project"] = project["id"]
        ledger["batch_job"] = job["id"]
        LEDGER.write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"project": project["id"], "job": job["id"], "samplers": 1}), flush=True)
        deadline = time.monotonic() + 1200
        last = None
        while time.monotonic() < deadline:
            job = await call("/jobs/" + job["id"])
            status = (job["status"], job["message"])
            if status != last:
                print(status, flush=True)
                last = status
            if job["status"] == "completed":
                assert {o["creative_node"] for o in job["outputs"]} == {"source", "up2", "up4"}
                ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
                ledger["tests"].append({"name": "共享图片→2×/4×超分", "job_id": job["id"],
                                        "status": "completed", "outputs": job["outputs"],
                                        "seconds": job["ended_at"] - job["created_at"], "samplers": 1})
                ledger["full_connect_verified"] = True
                ledger["same_session_reconnect_verified"] = machine["session"]
                LEDGER.write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")
                print(json.dumps(job["outputs"], ensure_ascii=False), flush=True)
                return
            if job["status"] in {"failed", "cancelled", "recovering"}:
                raise RuntimeError(job["message"])
            await asyncio.sleep(5)
        raise TimeoutError("Batch acceptance timed out")


if __name__ == "__main__":
    asyncio.run(main())
