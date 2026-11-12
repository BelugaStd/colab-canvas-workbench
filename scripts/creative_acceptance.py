"""Paid acceptance run, explicitly invoked by the operator; capped below 50 CU."""
import asyncio
import json
import time
import uuid
from pathlib import Path

import aiohttp

DIRECTORY = Path(__file__).resolve().parents[2] / "creative-validation"
BASE = "http://127.0.0.1:8777/api"


async def main():
    ledger_path = DIRECTORY / "cu-ledger.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    started = ledger["started_at"]
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as http:
        async with http.get(BASE + "/state") as response:
            token = (await response.json())["token"]

        async def call(path, body=None, method=None):
            async with http.request(method or ("GET" if body is None else "POST"), BASE + path,
                                    json=body, headers={"X-Workbench-Token": token}) as response:
                data = await response.json()
                if response.status >= 400:
                    raise RuntimeError(str(data))
                return data

        def save():
            ledger_path.write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")

        async def wait(job):
            last = None
            while True:
                # A two-hour wall limit leaves ample room below the 50 CU authorization.
                if time.time() - started > 7200:
                    raise TimeoutError("Real acceptance wall-time budget reached")
                job = await call('/jobs/' + job['id'])
                state = await call('/state')
                machine = next(a['machine'] for a in state['accounts'] if a['id'] == 'colab1')
                session = machine.get('session')
                if session and session not in ledger['owned_sessions']:
                    ledger['owned_sessions'].append(session)
                    save()
                message = (machine['phase'], machine['message'], job['status'], job['message'])
                if message != last:
                    print(' | '.join(message), flush=True)
                    last = message
                if job['status'] == 'completed':
                    ledger['tests'].append({'job_id': job['id'], 'name': job['name'], 'status': job['status'],
                                            'seconds': job['ended_at'] - job['created_at'], 'outputs': job['outputs']})
                    save()
                    return job
                if job['status'] in {'failed', 'cancelled', 'recovering'}:
                    raise RuntimeError(f"{job['name']}: {job['message']}")
                await asyncio.sleep(5)

        try:
            state = await call('/state')
            initial = next(j for j in state['jobs'] if j.get('creative') and j['status'] == 'completed' and j['creative']['target_node'] not in {'moss', 'moss-edit', 'upscale', 'video', 'loop'})
            image_asset = next(o['asset_id'] for o in initial['outputs'] if o.get('asset_id'))
            project = await call('/projects/' + ledger['acceptance_project']) if ledger.get('acceptance_project') else await call('/projects', {'name': 'Colab 实机验收 · 图片 视频 超分'})
            ledger['acceptance_project'] = project['id']
            canvas = project['canvases'][0]
            if not any(n['id'] == 'reference' for n in canvas['nodes']):
                canvas['nodes'].append({'id': 'reference', 'type': 'creative', 'position': {'x': 0, 'y': 0},
                                       'data': {'kind': 'upload', 'label': '狐狸参考', 'asset_id': image_asset, 'media_type': 'image/png'}})

            async def generate(key, label, kind, reference=False, **settings):
                nonlocal project, canvas
                if any(t['name'] == label and t['status'] == 'completed' for t in ledger['tests']):
                    return
                node = {'id': key, 'type': 'creative', 'position': {'x': 500 * len(canvas['nodes']), 'y': 0},
                        'data': {'kind': kind, 'label': label, **settings}}
                canvas['nodes'] = [n for n in canvas['nodes'] if n['id'] != key] + [node]
                canvas['edges'] = [e for e in canvas['edges'] if e['target'] != key]
                if reference:
                    canvas['edges'].append({'id': uuid.uuid4().hex, 'source': 'reference', 'target': key})
                project = await call('/projects/' + project['id'], project, 'PUT')
                canvas = project['canvases'][0]
                job = await call(f"/projects/{project['id']}/canvases/{canvas['id']}/nodes/{key}/generate", {'account': 'colab1'})
                result = await wait(job)
                output = next(o for o in result['outputs'] if o.get('creative_node') == key)
                target = next(n for n in canvas['nodes'] if n['id'] == key)
                target['data'].update(asset_id=output['asset_id'], media_type=output['media_type'], job_id=job['id'])
                project = await call('/projects/' + project['id'], project, 'PUT')
                canvas = project['canvases'][0]

            await generate('moss', 'MOSS 文生图', 'image', model='moss-image', resolution='1K', aspect='1:1', quality='标准',
                           prompt='A watercolor painting of a red fox sitting beside a peaceful lake, pine trees and morning mist, no text.')
            await generate('moss-edit', 'MOSS 参考编辑', 'image', reference=True, model='moss-image', resolution='1K', aspect='1:1', quality='标准',
                           prompt='Change the lighting to a cool moonlit evening, preserve the fox and the lake composition.')
            await generate('upscale', 'Real-ESRGAN 2×', 'upscale', reference=True, resolution='2×')
            await generate('video', 'H3 首帧 · 音频', 'video', reference=True, resolution='512p', aspect='16:9', duration=5, fps=24, audio=True, mode='首帧',
                           prompt='The fox gently turns its head as lake ripples move slowly. The camera stays still. Quiet natural forest ambience.')
            await generate('loop', 'H3 十秒循环', 'video', reference=True, resolution='512p', aspect='16:9', duration=10, fps=30, audio=False, mode='首尾循环',
                           prompt='A seamless living wallpaper. Subtle breathing of the fox, slow drifting mist and gentle lake ripples. Locked camera, no cuts, return to the exact initial pose and composition.')
            ledger['success'] = True
            save()
        except BaseException as exc:
            ledger['error'] = str(exc)
            save()
            raise
        finally:
            state = await call('/state')
            for job in state['jobs']:
                if job['account'] == 'colab1' and job['status'] in {'pending', 'preparing', 'submitting', 'queued', 'running', 'recovering'}:
                    try:
                        await call('/jobs/' + job['id'] + '/cancel', {})
                    except RuntimeError:
                        pass
            await call('/accounts/colab1/stop', {})
            for _ in range(60):
                state = await call('/state')
                machine = next(a['machine'] for a in state['accounts'] if a['id'] == 'colab1')
                if not machine.get('session'):
                    ledger['shutdown_verified'] = True
                    break
                await asyncio.sleep(3)
            ledger['finished_at'] = time.time()
            ledger['final'] = await call('/accounts/colab1/refresh', {})
            ledger['gross_balance_change'] = ledger['baseline']['usage']['balance'] - ledger['final']['usage']['balance']
            save()
            print(json.dumps({k: ledger.get(k) for k in ('success', 'error', 'shutdown_verified', 'gross_balance_change')}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    asyncio.run(main())
