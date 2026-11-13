import asyncio
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from backend import file_transfer


@contextmanager
def server(responder):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            responder(self)

        def log_message(self, *args):
            pass

    http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{http.server_port}'
    finally:
        http.shutdown()
        http.server_close()
        thread.join(timeout=2)


def reply(handler, data, status=200, headers=None):
    handler.send_response(status)
    for key, value in {'Content-Type': 'image/png', 'Content-Length': str(len(data)), **(headers or {})}.items():
        handler.send_header(key, value)
    handler.end_headers()
    handler.wfile.write(data)


def test_private_download_streams_byte_exact_and_atomically_finishes(tmp_path):
    data = b'\x89PNG\r\n\x1a\n' + bytes(range(256)) * 5000
    target = tmp_path / 'image.png'
    seen = []
    def respond(handler):
        seen.append(handler.path)
        assert not target.exists()
        reply(handler, data)
    with server(respond) as base:
        result = file_transfer.download(base, 'private-test-token', 'content/colab-workbench/ComfyUI/output/a.png', target)
    assert target.read_bytes() == data
    assert result['bytes'] == len(data)
    assert '/files/content/colab-workbench/ComfyUI/output/a.png?' in seen[0]
    assert 'private-test-token' in seen[0]
    assert not target.with_suffix('.png.part').exists()
    assert not target.with_suffix('.png.part.json').exists()


def test_disconnected_private_download_resumes_exact_range(tmp_path, monkeypatch):
    data = b'\x89PNG\r\n\x1a\n' + bytes(range(256)) * 5000
    seen = []
    monkeypatch.setattr(file_transfer.time, 'sleep', lambda _: None)
    def respond(handler):
        offset = len(seen)
        seen.append(dict(handler.headers))
        if offset == 0:
            handler.send_response(200)
            handler.send_header('Content-Type', 'image/png')
            handler.send_header('Content-Length', str(len(data)))
            handler.send_header('ETag', '"v1"')
            handler.end_headers()
            handler.wfile.write(data[:40000])
            handler.wfile.flush()
            handler.close_connection = True
        else:
            start = int(handler.headers['Range'].split('=')[1].split('-')[0])
            assert start == 40000
            assert handler.headers['If-Range'] == '"v1"'
            reply(handler, data[start:], 206, {'Content-Range': f'bytes {start}-{len(data) - 1}/{len(data)}', 'ETag': '"v1"'})
    with server(respond) as base:
        result = file_transfer.download(base, 'hidden', 'content/colab-workbench/ComfyUI/output/a.png', tmp_path / 'image.png')
    assert result['attempts'] == 2 and len(seen) == 2
    assert (tmp_path / 'image.png').read_bytes() == data


def test_server_ignoring_range_restarts_file_without_duplicate_prefix(tmp_path, monkeypatch):
    data = b'\x89PNG\r\n\x1a\n' + b'x' * 100000
    target = tmp_path / 'image.png'
    file_transfer.save_partial(target, 'content/colab-workbench/ComfyUI/output/a.png', len(data), '"v1"')
    target.with_suffix('.png.part').write_bytes(data[:20000])
    seen = []
    def respond(handler):
        seen.append(handler.headers.get('Range'))
        reply(handler, data, headers={'ETag': '"v2"'})
    with server(respond) as base:
        file_transfer.download(base, 'hidden', 'content/colab-workbench/ComfyUI/output/a.png', target)
    assert seen == ['bytes=20000-']
    assert target.read_bytes() == data


@pytest.mark.parametrize('content_range,length', [('bytes 10-19/20', '10'), ('bytes 0-9/20', '10'),
    ('bytes 4-12/10', '9'), ('bytes 4-9/10', '5')])
def test_invalid_partial_response_never_appends(tmp_path, content_range, length):
    target = tmp_path / 'image.png'
    remote = 'content/colab-workbench/ComfyUI/output/a.png'
    file_transfer.save_partial(target, remote, 10, '"v1"')
    target.with_suffix('.png.part').write_bytes(b'HEAD')
    def respond(handler):
        reply(handler, b'bad-data!!', 206, {'Content-Range': content_range, 'Content-Length': length})
    with server(respond) as base:
        with pytest.raises(file_transfer.TransferError):
            file_transfer.download(base, 'hidden', remote, target)
    assert not target.exists()
    assert target.with_suffix('.png.part').read_bytes() == b'HEAD'


@pytest.mark.parametrize('status,mime,data', [(302, 'text/html', b'redirect'),
    (200, 'text/html', b'<html>Sign in</html>'), (200, 'application/octet-stream', b'<!doctype html><html>Sign in</html>')])
def test_redirect_or_html_does_not_leak_token_or_create_media(tmp_path, status, mime, data):
    hits = []
    def respond(handler):
        hits.append(handler.path)
        reply(handler, data, status, {'Content-Type': mime, 'Location': 'http://127.0.0.1:1/unsafe'})
    with server(respond) as base:
        with pytest.raises(file_transfer.TransferError) as error:
            file_transfer.download(base, 'must-never-appear', 'content/colab-workbench/ComfyUI/output/a.png', tmp_path / 'image.png')
    assert 'must-never-appear' not in str(error.value)
    assert len(hits) == 1 and not (tmp_path / 'image.png').exists()


@pytest.mark.parametrize('remote', ['../secret', '/etc/passwd', 'content/colab-workbench/ComfyUI/output/../secret',
    'content/colab-workbench/ComfyUI/output/..%2fsecret', 'https://example.org/file'])
def test_private_transfer_rejects_unsafe_remote_paths_without_request(tmp_path, remote):
    with pytest.raises(file_transfer.TransferError):
        file_transfer.download('https://example.invalid', 'hidden', remote, tmp_path / 'image.png')


def test_timeout_budget_is_idle_timeout_not_total_file_deadline(tmp_path, monkeypatch):
    settings = []
    class Response:
        status = 200
        headers = {'Content-Length': '4', 'Content-Type': 'video/mp4'}
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, size):
            settings.append(size)
            return b'data' if len(settings) == 1 else b''
    class Opener:
        def open(self, request, timeout):
            assert timeout == file_transfer.IDLE_TIMEOUT
            return Response()
    monkeypatch.setattr(file_transfer, 'build_opener', lambda *args: Opener())
    file_transfer.download('https://example.invalid', 'hidden', 'content/colab-workbench/ComfyUI/output/a.mp4', tmp_path / 'video.mp4')
    assert (tmp_path / 'video.mp4').read_bytes() == b'data'


async def test_colab_cancel_kills_only_download_child_and_cleans_partial(tmp_path, monkeypatch):
    from backend import colab

    started = asyncio.Event()
    killed = []
    target = tmp_path / 'image.png'
    class Process:
        returncode = None
        async def communicate(self):
            target.with_suffix('.png.part').write_bytes(b'partial')
            target.with_suffix('.png.part.json').write_text('{}')
            started.set()
            await asyncio.Event().wait()
    process = Process()
    async def launch(*args, **kwargs):
        assert all('private-test-token' not in str(arg) for arg in args)
        return process
    async def stop(value):
        killed.append(value)
        value.returncode = -1
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', launch)
    monkeypatch.setattr(colab, 'stop_process', stop)
    monkeypatch.setattr(colab, 'launcher_python', lambda _: 'python')
    adapter = colab.Colab([{'id': 'a', 'launcher': 'trusted-launcher'}], tmp_path)
    task = asyncio.create_task(adapter.download('a', 'existing-session', 'content/colab-workbench/ComfyUI/output/a.png', target))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert killed == [process]
    assert not target.exists() and not target.with_suffix('.png.part').exists()
    assert not target.with_suffix('.png.part.json').exists()


def test_existing_partial_identity_cannot_resume_a_different_source(tmp_path):
    target = tmp_path / 'image.png'
    file_transfer.save_partial(target, 'content/colab-workbench/ComfyUI/output/other.png', 20, '"old"')
    target.with_suffix('.png.part').write_bytes(b'not-this-image')
    data = b'new-file'
    ranges = []
    def respond(handler):
        ranges.append(handler.headers.get('Range'))
        reply(handler, data)
    with server(respond) as base:
        file_transfer.download(base, 'hidden', 'content/colab-workbench/ComfyUI/output/a.png', target)
    assert ranges == [None] and target.read_bytes() == data


def test_etag_change_on_partial_response_is_rejected(tmp_path):
    target = tmp_path / 'image.png'
    remote = 'content/colab-workbench/ComfyUI/output/a.png'
    file_transfer.save_partial(target, remote, 10, '"v1"')
    target.with_suffix('.png.part').write_bytes(b'HEAD')
    def respond(handler):
        reply(handler, b'tail!!', 206, {'Content-Range': 'bytes 4-9/10', 'ETag': '"v2"'})
    with server(respond) as base:
        with pytest.raises(file_transfer.TransferError):
            file_transfer.download(base, 'hidden', remote, target)
    assert not target.exists() and target.with_suffix('.png.part').read_bytes() == b'HEAD'


def transfer_manager(tmp_path, download):
    from backend.manager import Manager
    from backend.store import Store

    store = Store(tmp_path)
    manager = Manager(store, SimpleNamespace(download=download), [{'id': 'a'}])
    manager.machine('a', phase='ready', session='owned-session', port=12345)
    store.put('jobs', 'job', {'id': 'job', 'account': 'a', 'graph': {}})
    return manager, store


async def test_manager_prefers_private_transfer_without_opening_ssh_media_connection(tmp_path):
    calls = []
    async def download(account, session, remote, destination):
        calls.append((account, session, remote))
        destination.write_bytes(b'media')
    manager, store = transfer_manager(tmp_path, download)
    try:
        outputs = await manager.download_outputs('a', 'job', {'out': {'images': [
            {'filename': 'a.png', 'subfolder': 'workbench', 'type': 'output'}]}})
        assert outputs[0]['bytes'] == 5
        assert calls == [('a', 'owned-session', 'content/colab-workbench/ComfyUI/output/workbench/a.png')]
        assert manager.http is None
    finally:
        store.close()


async def test_manager_observable_single_fallback_has_idle_timeout_and_atomic_file(tmp_path):
    calls = []
    async def download(*args):
        calls.append('private')
        raise RuntimeError('私有媒体通道失败（HTTP 403）')
    class Content:
        async def iter_chunked(self, size):
            yield b'original-ssh'
    class Response:
        content = Content()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        def raise_for_status(self):
            pass
    class HTTP:
        def get(self, url, params, timeout):
            calls.append('ssh')
            assert timeout.total == 7200 and timeout.sock_connect == 20 and timeout.sock_read == 90
            return Response()
    manager, store = transfer_manager(tmp_path, download)
    manager.http = HTTP()
    try:
        result = await manager.download_outputs('a', 'job', {'out': {'images': [{'filename': 'a.png'}]}})
        assert calls == ['private', 'ssh']
        assert result[0]['bytes'] == len(b'original-ssh')
        assert '私有媒体下载不可用' in store.get('logs', 'a')[0]['message']
        assert (tmp_path / 'outputs/job/00-a.png').read_bytes() == b'original-ssh'
        assert not (tmp_path / 'outputs/job/00-a.png.part').exists()
    finally:
        store.close()


async def test_manager_preserves_completed_file_when_later_download_fails_then_resumes(tmp_path):
    import aiohttp

    seen, fail = [], True
    async def download(account, session, remote, destination):
        seen.append(remote)
        if remote.endswith('b.png') and fail:
            raise RuntimeError('private temporarily unavailable')
        destination.write_bytes(b'media')
    class HTTP:
        def get(self, *args, **kwargs):
            raise aiohttp.ClientConnectionError('ssh unavailable')
    manager, store = transfer_manager(tmp_path, download)
    manager.http = HTTP()
    outputs = {'out': {'images': [{'filename': 'a.png'}, {'filename': 'b.png'}]}}
    try:
        with pytest.raises(aiohttp.ClientConnectionError):
            await manager.download_outputs('a', 'job', outputs)
        first = tmp_path / 'outputs/job/00-a.png'
        assert first.read_bytes() == b'media'
        stamp = first.stat().st_mtime_ns
        fail = False
        result = await manager.download_outputs('a', 'job', outputs)
        assert len(result) == 2 and first.stat().st_mtime_ns == stamp
        assert sum(remote.endswith('a.png') for remote in seen) == 1
        assert sum(remote.endswith('b.png') for remote in seen) == 2
    finally:
        store.close()


async def test_manager_cancelling_private_download_never_starts_fallback(tmp_path):
    async def download(*args):
        raise asyncio.CancelledError()
    manager, store = transfer_manager(tmp_path, download)
    try:
        with pytest.raises(asyncio.CancelledError):
            await manager.download_outputs('a', 'job', {'out': {'images': [{'filename': 'a.png'}]}})
        assert not store.get('logs', 'a')
    finally:
        store.close()


async def test_download_recovery_always_exports_from_original_instead_of_exporting_previous_export(tmp_path, monkeypatch):
    from backend import manager as manager_module

    downloads, exports = [], []
    async def download(account, session, remote, destination):
        downloads.append(remote)
        destination.write_bytes(b'original')
    async def export(source, settings):
        exports.append(source.name)
        assert source.read_bytes() == b'original'
        target = source.with_name(source.stem + '.export.mp4')
        target.write_bytes(b'processed')
        return target
    async def register(store, destination, key, node):
        assert destination.name.endswith('.export.mp4')
        return {'id': 'asset', 'media_type': 'video/mp4'}
    monkeypatch.setattr(manager_module, 'export_video', export)
    monkeypatch.setattr(manager_module, 'register_result', register)
    manager, store = transfer_manager(tmp_path, download)
    store.patch('jobs', 'job', creative={'output_nodes': {'out': 'video'}, 'postprocess_nodes': {'video': {'fps': 24, 'duration': 10}}})
    outputs = {'out': {'videos': [{'filename': 'clip.mp4'}]}}
    try:
        first = await manager.download_outputs('a', 'job', outputs)
        second = await manager.download_outputs('a', 'job', outputs)
        assert first == second
        assert len(downloads) == 1
        assert exports == ['00-clip.mp4', '00-clip.mp4']
        assert not list((tmp_path / 'outputs/job').glob('*.export.export.mp4'))
    finally:
        store.close()
