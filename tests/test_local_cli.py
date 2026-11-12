import asyncio
import json
import os
import sys

import pytest

from backend.colab import Colab


def launcher_at(directory, source):
    launcher = directory / "account with spaces"
    launcher.write_text(f"#!{sys.executable}\n{source}\n", encoding="utf-8")
    if os.name != "nt":
        launcher.chmod(0o700)
    return launcher


async def test_python_launcher_preserves_argument_boundaries(tmp_path):
    launcher = launcher_at(tmp_path, "import json, sys; print(json.dumps(sys.argv[1:]))")
    cli = Colab([{"id": "colab1", "launcher": str(launcher)}], tmp_path)
    arguments = ("a b", 'quote"inside', "&not-a-shell-command", "中文")
    assert json.loads(await cli.command("colab1", *arguments)) == list(arguments)


async def test_timed_out_launcher_is_stopped(tmp_path):
    launcher = launcher_at(tmp_path, "import time; time.sleep(60)")
    cli = Colab([{"id": "colab1", "launcher": str(launcher)}], tmp_path)
    with pytest.raises(TimeoutError):
        await cli.command("colab1", timeout=0.1)


async def test_tunnel_does_not_return_before_ssh_forward_is_listening(tmp_path, monkeypatch):
    from backend import colab

    launcher = launcher_at(tmp_path, 'pass')
    identity = tmp_path / 'key'
    identity.touch()
    create = asyncio.create_subprocess_exec
    processes = []
    async def delayed_failure(*args, **kwargs):
        if args[0] != 'ssh':
            return await create(*args, **kwargs)
        process = await create(sys.executable, '-c',
                               "import time,sys;time.sleep(1.1);print('SSH authentication failed',file=sys.stderr);sys.exit(1)",
                               **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(colab.asyncio, 'create_subprocess_exec', delayed_failure)
    cli = Colab([{'id': 'colab1', 'launcher': str(launcher)}], tmp_path)
    try:
        with pytest.raises(RuntimeError, match='SSH authentication failed'):
            await cli.tunnel('colab1', 'existing', str(identity))
    finally:
        await cli.close()
        for process in processes:
            await process.wait()


async def test_tunnel_retries_colab_ssh_lease_on_same_runtime(tmp_path, monkeypatch):
    from backend import colab

    launcher = launcher_at(tmp_path, 'pass')
    identity = tmp_path / 'key'
    identity.touch()
    create = asyncio.create_subprocess_exec
    attempts = []
    async def lease_then_listen(*args, **kwargs):
        if args[0] != 'ssh':
            return await create(*args, **kwargs)
        attempts.append(args)
        if len(attempts) == 1:
            code = "import sys;print('Already-active SSH session (HTTP 429)',file=sys.stderr);sys.exit(1)"
        else:
            forwarding = args[args.index('-L') + 1]
            port = int(forwarding.split(':')[1])
            code = f"import socket,time;s=socket.socket();s.bind(('127.0.0.1',{port}));s.listen();time.sleep(60)"
        return await create(sys.executable, '-c', code, **kwargs)
    monkeypatch.setattr(colab.asyncio, 'create_subprocess_exec', lease_then_listen)
    cli = Colab([{'id': 'colab1', 'launcher': str(launcher)}], tmp_path)
    try:
        port = await cli.tunnel('colab1', 'original-runtime', str(identity))
        assert len(attempts) == 2
        assert all('original-runtime' in ' '.join(args) for args in attempts)
        reader, writer = await asyncio.open_connection('127.0.0.1', port)
        writer.close()
        await writer.wait_closed()
    finally:
        await cli.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows process termination")
async def test_closing_windows_tunnel_reaps_process(tmp_path):
    cli = Colab([], tmp_path)
    process = await asyncio.create_subprocess_exec(sys.executable, "-c", "import time; time.sleep(60)")
    cli.tunnels["colab1"] = process
    try:
        await cli.close_tunnel("colab1")
        assert process.returncode is not None
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


@pytest.mark.skipif(os.name != "nt", reason="Windows launcher discovery")
def test_local_accounts_are_discovered_without_reading_credentials(tmp_path, monkeypatch):
    from backend import config

    monkeypatch.setattr(config, "DATA", tmp_path)
    monkeypatch.setattr(config.shutil, "which", lambda name: None)
    directory = tmp_path / "colab-accounts"
    (directory / "bin").mkdir(parents=True)
    (directory / "credentials").mkdir()
    (directory / "bin" / "colab1").write_text("# launcher", encoding="utf-8")
    (directory / "credentials" / "1.json").write_text("not read by discovery", encoding="utf-8")
    accounts = config.profiles()
    assert accounts[0]["installed"] is True
    assert accounts[0]["credential_present"] is True
    assert accounts[1]["credential_present"] is False


@pytest.mark.skipif(os.name != "nt", reason="Windows OpenSSH argument parsing")
async def test_windows_ssh_proxy_uses_windows_quoting_and_null_device(tmp_path, monkeypatch):
    import subprocess
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from backend import colab

    launcher = launcher_at(tmp_path, "pass")
    identity = tmp_path / "key with spaces"
    identity.touch()
    process = SimpleNamespace(returncode=None)
    create = AsyncMock(return_value=process)
    monkeypatch.setattr(colab.asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(colab.asyncio, "sleep", AsyncMock())
    writer = SimpleNamespace(close=lambda: None, wait_closed=AsyncMock())
    monkeypatch.setattr(colab.asyncio, "open_connection", AsyncMock(return_value=(None, writer)))
    cli = Colab([{"id": "colab1", "launcher": str(launcher)}], tmp_path)
    await cli.tunnel("colab1", "existing", str(identity))
    args = create.call_args.args
    expected = subprocess.list2cmdline([sys.executable, str(colab.ROOT / "backend/bridge.py"),
                                      str(launcher), "existing", str(identity)])
    assert f"ProxyCommand={expected}" in args
    assert f"UserKnownHostsFile={os.devnull}" in args


async def test_new_local_login_is_detected_without_server_restart(tmp_path, monkeypatch):
    from httpx import ASGITransport, AsyncClient

    from backend import app as app_module
    from tests.fake_colab import PROFILES, FakeColab

    credential_marker = tmp_path / "login-present"

    def discover():
        return [{**profile, "credential_present": credential_marker.exists()} for profile in PROFILES]

    monkeypatch.setattr(app_module, "profiles", discover)
    app = app_module.create_app(tmp_path, driver=FakeColab())
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            assert not (await client.get("/api/state")).json()["accounts"][0]["credential_present"]
            credential_marker.touch()
            assert (await client.get("/api/state")).json()["accounts"][0]["credential_present"]
