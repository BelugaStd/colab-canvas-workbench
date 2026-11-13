"""Stream existing Colab files through its authenticated runtime proxy."""
import http.client
import json
import re
import sys
import time
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

IDLE_TIMEOUT = 60
MAX_ATTEMPTS = 3
CHUNK_SIZE = 256 * 1024
RETRY_STATUS = {408, 429, 500, 502, 503, 504}


class TransferError(RuntimeError):
    def __init__(self, message, retryable=False):
        super().__init__(message)
        self.retryable = retryable


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def remote_path(value):
    if not isinstance(value, str) or '\\' in value or '\0' in value or '%' in value:
        raise TransferError('私有媒体路径无效')
    relative = value.removeprefix('/')
    pieces = relative.split('/')
    if (len(pieces) < 5 or any(part in {'', '.', '..'} for part in pieces) or unquote(relative) != relative
            or not relative.startswith('content/colab-workbench/ComfyUI/')
            or pieces[3] not in {'input', 'output', 'temp'}):
        raise TransferError('私有媒体路径超出工作台目录')
    return str(PurePosixPath(relative))


def partial_paths(destination):
    destination = Path(destination)
    return destination.with_suffix(destination.suffix + '.part'), destination.with_suffix(destination.suffix + '.part.json')


def save_partial(destination, remote, total, etag):
    _, metadata = partial_paths(destination)
    metadata.write_text(json.dumps({'remote': remote, 'total': total, 'etag': etag}), encoding='utf-8')


def partial_state(destination, remote):
    partial, metadata = partial_paths(destination)
    try:
        saved = json.loads(metadata.read_text(encoding='utf-8'))
        offset = partial.stat().st_size
        if (saved.get('remote') == remote and type(saved.get('total')) is int
                and 0 < offset < saved['total'] and isinstance(saved.get('etag', ''), str)):
            return offset, saved['total'], saved.get('etag', '')
    except (OSError, ValueError, TypeError):
        pass
    partial.unlink(missing_ok=True)
    metadata.unlink(missing_ok=True)
    return 0, None, ''


def response_shape(response, offset, previous_total, previous_etag):
    status = response.status
    content_type = response.headers.get('Content-Type', '').split(';')[0].strip().lower()
    if content_type.startswith('text/') or content_type in {'application/json', 'application/xhtml+xml'}:
        raise TransferError('私有文件服务返回了非媒体页面')
    try:
        length = int(response.headers['Content-Length'])
    except (KeyError, TypeError, ValueError):
        raise TransferError('私有文件响应缺少有效长度') from None
    if length <= 0:
        raise TransferError('私有文件响应为空')
    etag = response.headers.get('ETag', '')
    if status == 206:
        matched = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', response.headers.get('Content-Range', ''))
        if not matched:
            raise TransferError('私有文件分段响应范围无效')
        start, end, total = map(int, matched.groups())
        if (start != offset or not start <= end < total or end - start + 1 != length
                or previous_total is not None and total != previous_total
                or previous_etag and etag != previous_etag):
            raise TransferError('私有文件分段偏移、长度或版本不一致')
        return offset, total, etag, end + 1
    if status != 200:
        raise TransferError(f'私有文件服务返回 HTTP {status}', status in RETRY_STATUS)
    return 0, length, etag, length


def download(base_url, token, source, destination):
    """Stream to .part and resume only validated ranges; never log the proxy URL."""
    source = remote_path(source)
    endpoint = urlsplit(base_url)
    if (endpoint.scheme not in {'http', 'https'} or not endpoint.hostname or endpoint.username or endpoint.password
            or endpoint.query or endpoint.fragment or endpoint.scheme == 'http' and endpoint.hostname not in {'127.0.0.1', 'localhost', '::1'}):
        raise TransferError('私有运行时文件服务地址无效')
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial, metadata = partial_paths(destination)
    opener = build_opener(NoRedirect())
    url = base_url.rstrip('/') + '/files/' + quote(source, safe='/') + '?' + urlencode({
        'authuser': '0', 'colab-runtime-proxy-token': token})
    last_error = None
    started = time.monotonic()
    for attempt in range(MAX_ATTEMPTS):
        offset, total, etag = partial_state(destination, source)
        headers = {'Accept': 'application/octet-stream', 'Accept-Encoding': 'identity'}
        if offset:
            headers['Range'] = f'bytes={offset}-'
            if etag:
                headers['If-Range'] = etag
        try:
            with opener.open(Request(url, headers=headers), timeout=IDLE_TIMEOUT) as response:
                start, total, etag, response_end = response_shape(response, offset, total, etag)
                save_partial(destination, source, total, etag)
                written = start
                with partial.open('ab' if start else 'wb') as output:
                    while True:
                        try:
                            chunk = response.read(CHUNK_SIZE)
                        except http.client.IncompleteRead as exc:
                            if exc.partial:
                                output.write(exc.partial)
                            raise TransferError('私有媒体连接在文件完成前中断', True) from None
                        if not chunk:
                            break
                        if written == 0 and chunk.lstrip()[:64].lower().startswith((b'<!doctype html', b'<html')):
                            raise TransferError('私有文件服务返回了登录页面')
                        output.write(chunk)
                        written += len(chunk)
                        if written > response_end:
                            raise TransferError('私有文件实际长度超过响应范围')
                if written != response_end or written != total:
                    raise TransferError('私有媒体连接在文件完成前中断', True)
            if partial.stat().st_size != total:
                raise TransferError('私有媒体实际文件长度校验失败')
            partial.replace(destination)
            metadata.unlink(missing_ok=True)
            return {'bytes': total, 'attempts': attempt + 1, 'elapsed_seconds': round(time.monotonic() - started, 3)}
        except HTTPError as exc:
            last_error = TransferError(f'私有文件服务返回 HTTP {exc.code}', exc.code in RETRY_STATUS)
            exc.close()
        except (URLError, TimeoutError, ConnectionError, http.client.HTTPException):
            last_error = TransferError('私有媒体通道连接中断或读取超时', True)
        except TransferError as exc:
            last_error = exc
        if not last_error.retryable or attempt + 1 == MAX_ATTEMPTS:
            raise last_error from None
        time.sleep(2 ** attempt)
    raise last_error


def main():
    from colab_cli.contents import ContentsClient

    launcher, session, source, destination = sys.argv[1:]
    def streaming_download(client, remote, local):
        download(client.base_url, client.token, remote, local)
    ContentsClient.download = streaming_download
    sys.argv = [launcher, 'download', '-s', session, source, destination]
    trusted = Path(launcher).resolve()
    exec(compile(trusted.read_bytes(), str(trusted), 'exec'), {'__name__': '__main__', '__file__': str(trusted)})


if __name__ == '__main__':
    main()
