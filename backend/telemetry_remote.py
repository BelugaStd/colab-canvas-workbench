"""Loopback Colab sidecar for telemetry and pinned model preparation."""
import csv
import json
import math
import os
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def gpu_sample():
    fields = "name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,power.limit"
    result = subprocess.run(["nvidia-smi", f"--query-gpu={fields}", "--format=csv,noheader,nounits"],
                            capture_output=True, text=True, timeout=2, check=True)
    values = next(csv.reader(result.stdout.splitlines()))
    name, utilization, used, total, temperature, power, limit = [v.strip() for v in values]
    used, total = number(used), number(total)
    return {"name": name, "percent": number(utilization),
            "memory_used": used * 2**20 if used is not None else None,
            "memory_total": total * 2**20 if total is not None else None,
            "memory_percent": 100 * used / total if used is not None and total else None,
            "temperature": number(temperature), "power": number(power), "power_limit": number(limit)}


class ModelPreparation:
    def __init__(self, root):
        self.root = Path(root)
        self.lock = threading.Lock()

    def path(self, key):
        if not re.fullmatch(r"[0-9a-f]{32}", key):
            raise ValueError("Invalid preparation ID")
        return self.root / f"models-{key}.json"

    def read(self, key):
        path = self.path(key)
        return json.loads(path.read_text()) if path.exists() else {"phase": "missing"}

    def start(self, key, models):
        if __package__:
            from .deploy_remote import MODEL_GROUPS, models_ready
        else:
            from deploy_remote import MODEL_GROUPS, models_ready
        allowed = set(MODEL_GROUPS)
        if not isinstance(models, list) or not models or len(models) > len(allowed) or any(type(m) is not str or m not in allowed for m in models):
            raise ValueError("Invalid model preset")
        with self.lock:
            path = self.path(key)
            if path.exists():
                return self.read(key)
            if models_ready(self.root, models):
                result = {'phase': 'ready', 'message': '所需模型已缓存，直接复用', 'time': time.time()}
                path.write_text(json.dumps(result))
                return result
            path.write_text(json.dumps({"phase": "starting", "message": "准备所需模型", "time": time.time()}))
            try:
                with (self.root / f"models-{key}.log").open("ab") as log:
                    subprocess.Popen([str(self.root / "venv/bin/python"), str(self.root / "deploy_remote.py"),
                                      "--models-only", "--models", ",".join(models)],
                                     stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                     start_new_session=True, env={**os.environ, "WB_DEPLOY_STATUS": f"models-{key}"})
            except Exception as exc:
                path.write_text(json.dumps({"phase": "error", "message": f"{type(exc).__name__}: {exc}"}))
            return self.read(key)


class Sampler:
    def __init__(self, psutil, disk_path="/content"):
        self.psutil, self.disk_path = psutil, disk_path
        self.previous_net = None
        self.previous_time = None
        self.cpu_primed = False

    def sample(self):
        now = time.monotonic()
        p = self.psutil
        cpu = p.cpu_percent(interval=None)
        if not self.cpu_primed:
            cpu = None  # psutil's first nonblocking measurement has no baseline.
            self.cpu_primed = True
        ram, disk = p.virtual_memory(), p.disk_usage(self.disk_path)
        interfaces = p.net_io_counters(pernic=True)
        rx = sum(v.bytes_recv for name, v in interfaces.items() if name != "lo")
        tx = sum(v.bytes_sent for name, v in interfaces.items() if name != "lo")
        elapsed = now - self.previous_time if self.previous_time is not None else 0
        network = {"rx_bytes_s": None, "tx_bytes_s": None}
        if elapsed > 0 and self.previous_net is not None:
            network = {"rx_bytes_s": max(0, rx - self.previous_net[0]) / elapsed,
                       "tx_bytes_s": max(0, tx - self.previous_net[1]) / elapsed}
        self.previous_time, self.previous_net = now, (rx, tx)
        gpu, gpu_error = None, None
        try:
            gpu = gpu_sample()
        except (OSError, subprocess.SubprocessError, ValueError, StopIteration) as exc:
            gpu_error = type(exc).__name__
        return {"sampled_at": time.time(), "cpu": {"percent": cpu, "cores": p.cpu_count()},
                "memory": {"used": ram.total - ram.available, "total": ram.total, "percent": ram.percent},
                "disk": {"used": disk.used, "total": disk.total, "percent": disk.percent},
                "gpu": gpu, "gpu_error": gpu_error, "network": network}


def main():
    import fcntl

    import psutil

    lock = open(os.path.join(os.path.dirname(__file__), "telemetry.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return  # The existing sidecar owns the port.
    sampler = Sampler(psutil)
    cache = {"sample": None, "error": None}
    models = ModelPreparation(Path(__file__).resolve().parent)

    def collect():
        while True:
            start = time.monotonic()
            try:
                cache.update(sample=sampler.sample(), error=None)
            except Exception as exc:
                cache.update(sample=None, error=type(exc).__name__)
            time.sleep(max(0.1, 2 - (time.monotonic() - start)))

    class Handler(BaseHTTPRequestHandler):
        def reply(self, body, code=200):
            data = json.dumps(body, allow_nan=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path.startswith("/models/"):
                try:
                    self.reply(models.read(self.path.removeprefix("/models/")))
                except ValueError as exc:
                    self.reply({"error": str(exc)}, 400)
                return
            if self.path != "/metrics":
                self.send_error(404)
                return
            self.reply(cache)

        def do_POST(self):
            if not self.path.startswith("/models/"):
                self.send_error(404)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 4096:
                    raise ValueError("Invalid request size")
                body = json.loads(self.rfile.read(length))
                self.reply(models.start(self.path.removeprefix("/models/"), body.get("models")))
            except (ValueError, AttributeError) as exc:
                self.reply({"error": str(exc)}, 400)

        def log_message(self, *args):
            pass

    threading.Thread(target=collect, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", 8189), Handler).serve_forever()


if __name__ == "__main__":
    main()
