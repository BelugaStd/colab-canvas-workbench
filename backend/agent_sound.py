"""Local, sample-aligned sound editing with immutable speech and video evidence.

Filter contracts: https://ffmpeg.org/ffmpeg-filters.html (adelay, amix,
sidechaincompress, afade, loudnorm and silencedetect).
"""
import array
import asyncio
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import wave
from fractions import Fraction
from pathlib import Path

from .agent_media import binary, number, probe, register, run_command
from .assets import asset_path

RATE = 48000
REVISION = 'sound-mix-v1'
ROLES = {'dialogue', 'ambience', 'music', 'effect'}


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{32}', value):
        raise ValueError('音频操作只接受本地素材 ID')
    return value


def _text(value, name, limit):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= limit:
        raise ValueError(f'{name} 必须为 1–{limit} 个字符')
    return value.strip()


def _request(video_asset_id, cues, name, keep_source_audio):
    _id(video_asset_id)
    _text(name, '素材名称', 100)
    if type(keep_source_audio) is not bool:
        raise ValueError('keep_source_audio 必须为布尔值')
    if not isinstance(cues, list) or len(cues) > 256:
        raise ValueError('音频时间线最多 256 条音频')
    normalized = []
    for cue in cues:
        if not isinstance(cue, dict) or set(cue) - {
                'asset_id', 'start', 'gain_db', 'role', 'speaker', 'text', 'allow_overlap'}:
            raise ValueError('音频条目含不支持的参数')
        _id(cue.get('asset_id'))
        if not isinstance(cue.get('role'), str) or cue['role'] not in ROLES:
            raise ValueError('音频角色只支持 dialogue/ambience/music/effect')
        item = {'asset_id': cue['asset_id'], 'start': number(cue.get('start'), '音频起点', 0, 600),
                'gain_db': number(cue.get('gain_db', 0), '音量 dB', -24, 12), 'role': cue['role'],
                'allow_overlap': cue.get('allow_overlap', False)}
        if type(item['allow_overlap']) is not bool:
            raise ValueError('allow_overlap 必须为布尔值')
        for field, limit in [('speaker', 100), ('text', 4000)]:
            if field in cue:
                item[field] = _text(cue[field], field, limit)
        if cue['role'] != 'dialogue' and any(k in cue for k in ('speaker', 'text', 'allow_overlap')):
            raise ValueError('角色、台词与打断参数只适用于 dialogue')
        normalized.append(item)
    request = {'revision': REVISION, 'video_asset_id': video_asset_id, 'cues': normalized,
               'name': name, 'keep_source_audio': keep_source_audio}
    encoded = json.dumps(request, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    return normalized, hashlib.sha256(encoded).hexdigest()


async def _capture(args):
    process = await asyncio.create_subprocess_exec(*map(str, args), stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 1200)
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise
    if process.returncode:
        raise RuntimeError('音频媒体处理失败：' + stderr.decode(errors='replace')[-2500:])
    return stdout, stderr.decode(errors='replace')


async def _loudness(path):
    _, log = await _capture([binary('ffmpeg'), '-hide_banner', '-nostats', '-xerror', '-err_detect',
        'explode', '-i', path, '-map', '0:a:0', '-af',
        'silencedetect=noise=-55dB:d=0.1,loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json', '-f', 'null', '-'])
    found = re.findall(r'\{\s*"input_i".*?\}', log, re.S)
    if not found:
        raise RuntimeError('无法取得实际音频响度数据')
    measurement = json.loads(found[-1])
    values = {key: float(measurement[key]) for key in
              ('input_i', 'input_tp', 'input_lra', 'input_thresh', 'target_offset')}
    silences, start = [], None
    for event, value in re.findall(r'silence_(start|end):\s*(-?[\d.]+)', log):
        if event == 'start':
            start = float(value)
        elif start is not None:
            silences.append({'start': start, 'end': float(value)})
            start = None
    return values, silences, start


async def _decode_audio(path, destination):
    await run_command([binary('ffmpeg'), '-v', 'error', '-xerror', '-err_detect', 'explode', '-i', path,
        '-map', '0:a:0', '-vn', '-ar', RATE, '-ac', '2', '-c:a', 'pcm_s16le', destination])
    return await asyncio.to_thread(_wave_stats, destination)


def _wave_stats(destination):
    with wave.open(str(destination), 'rb') as stream:
        samples, channels = stream.getnframes(), stream.getnchannels()
        peak, energy, clipped, count = 0, 0, 0, 0
        while data := stream.readframes(32768):
            values = array.array('h')
            values.frombytes(data)
            if sys.byteorder != 'little':
                values.byteswap()
            peak = max(peak, max(map(abs, values), default=0))
            energy += sum(value * value for value in values)
            clipped += sum(abs(value) >= 32767 for value in values)
            count += len(values)
        if not samples or count != samples * channels:
            raise ValueError('音频未完整解码或数据被截断')
    return {'samples': samples, 'duration': samples / RATE, 'sample_rate': RATE,
            'peak_dbfs': 20 * math.log10(peak / 32768) if peak else None,
            'rms_dbfs': 10 * math.log10(energy / count / 32768**2) if energy else None,
            'clipped_sample_ratio': clipped / count}


async def _video_facts(path):
    info = await probe(path)
    video = next((item for item in info['streams'] if item['codec_type'] == 'video'), None)
    if not video:
        raise ValueError('混音需要包含实际视频轨道的素材')
    frames = video.get('nb_frames')
    if not frames or frames == 'N/A':
        counted = json.loads(await run_command([binary('ffprobe'), '-v', 'error', '-count_frames',
            '-select_streams', 'v:0', '-show_entries', 'stream=nb_read_frames', '-of', 'json', path]))
        frames = counted['streams'][0]['nb_read_frames']
    fps = Fraction(video.get('avg_frame_rate') or '0')
    if int(frames) <= 0 or fps <= 0:
        raise ValueError('视频帧数或帧率无效')
    if video.get('duration'):
        duration = float(video['duration'])
    else:
        packets = json.loads(await run_command([binary('ffprobe'), '-v', 'error', '-select_streams', 'v:0',
            '-show_packets', '-show_entries', 'packet=pts_time,duration_time', '-of', 'json', path]))['packets']
        bounds = [(float(item['pts_time']), float(item.get('duration_time', 1 / fps)))
                  for item in packets if 'pts_time' in item]
        if not bounds:
            raise ValueError('无法取得视频实际时间线')
        duration = max(start + length for start, length in bounds) - min(start for start, _ in bounds)
    number(duration, '视频时长', 1 / RATE, 600)
    return info, video, duration, int(frames), fps


async def _video_hash(path):
    return (await run_command([binary('ffmpeg'), '-v', 'error', '-i', path, '-map', '0:v:0',
        '-c:v', 'copy', '-f', 'hash', '-hash', 'sha256', '-'])).decode().strip()


async def mix(store, video_asset_id, cues, name, result_id=None, *, keep_source_audio=True):
    normalized, fingerprint = _request(video_asset_id, cues, name, keep_source_audio)
    if result_id:
        _id(result_id)
        existing = store.get('assets', result_id)
        if existing:
            existing_path, _ = asset_path(store, result_id)
            if existing.get('sound_timeline', {}).get('fingerprint') != fingerprint:
                raise ValueError('相同混音 ID 对应不同请求，不能覆盖既有素材')
            expected_hash = existing.get('sound_validation', {}).get('output_sha256')
            if not expected_hash or hashlib.sha256(existing_path.read_bytes()).hexdigest() != expected_hash:
                raise RuntimeError('已保存混音损坏或缺少校验记录，不能作为有效缓存复用')
            return existing
    source, asset = asset_path(store, video_asset_id)
    if not asset['media_type'].startswith('video/'):
        raise ValueError('混音输入必须为视频素材')
    info, video, duration, frames, fps = await _video_facts(source)
    total_samples = round(duration * RATE)
    source_audio = next((item for item in info['streams'] if item['codec_type'] == 'audio'), None)
    directory = store.directory / 'agent-media'
    directory.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='sound-', dir=directory) as temporary:
        temporary = Path(temporary)
        prepared, voices, decoded = [], {}, {}
        for index, cue in enumerate(normalized):
            path, audio = asset_path(store, cue['asset_id'])
            if not audio['media_type'].startswith('audio/'):
                raise ValueError('音频时间线只接受本地音频素材')
            speech = audio.get('agent_speech', {})
            if cue['role'] == 'dialogue':
                if not isinstance(speech, dict) or not speech.get('speaker') or not speech.get('text'):
                    raise ValueError('对白需要带可核实声音与台词元数据的配音素材')
                voice = _text(speech['speaker'], '配音声音', 100)
                text = _text(speech['text'], '配音台词', 4000)
                speaker = cue.get('speaker', voice)
                if cue.get('text', text) != text:
                    raise ValueError('台词与实际配音素材的元数据不一致')
                if speaker in voices and voices[speaker] != voice:
                    raise ValueError('同一角色使用了不同配音声音')
                voices[speaker] = voice
                if speech.get('sha256') and hashlib.sha256(path.read_bytes()).hexdigest() != speech['sha256']:
                    raise ValueError('配音素材内容与已保存的指纹不一致，可能损坏或被截断')
            if cue['asset_id'] not in decoded:
                audio_info = await probe(path)
                if not any(item['codec_type'] == 'audio' for item in audio_info['streams']):
                    raise ValueError('音频素材没有实际音轨')
                number(float(audio_info['format'].get('duration', 0)), '音频时长', 1 / RATE, 600)
                wav = temporary / f'cue-{index}.wav'
                stats = await _decode_audio(path, wav)
                decoded[cue['asset_id']] = wav, stats
            wav, stats = decoded[cue['asset_id']]
            if stats['rms_dbfs'] is None or stats['rms_dbfs'] < -70:
                raise ValueError('音频素材为静音或不可听，未注册混音结果')
            if cue['role'] == 'dialogue':
                if speech.get('samples') is not None and speech.get('sample_rate'):
                    expected = round(speech['samples'] / speech['sample_rate'] * RATE)
                    if abs(stats['samples'] - expected) > 1:
                        raise ValueError('配音实际样本数与元数据不一致，可能被截断')
                if stats['clipped_sample_ratio'] > .001:
                    raise ValueError('配音素材存在明显削波，不能作为成功对白')
            start = round(cue['start'] * RATE)
            if start + stats['samples'] > total_samples:
                raise ValueError('音频结束时间超过视频片长，禁止截断对白或音效')
            item = {**cue, **stats, 'start': start / RATE, 'start_sample': start,
                    'end_sample': start + stats['samples'], 'source_sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
            if cue['role'] == 'dialogue':
                item.update(speaker=speaker, voice=voice, text=text)
            prepared.append((wav, item))
        active = []
        for _, cue in sorted(prepared, key=lambda item: item[1]['start_sample']):
            if cue['role'] != 'dialogue':
                continue
            active = [previous for previous in active if previous['end_sample'] > cue['start_sample']]
            if active and not cue['allow_overlap']:
                raise ValueError('对白重叠；有意打断请在后一句明确 allow_overlap=true')
            active.append(cue)
        if not prepared and not (keep_source_audio and source_audio):
            raise ValueError('没有可混合的音频，输出会是静音')

        inputs = ['-i', source]
        filters, buses = [], {'dialogue': [], 'background': [], 'effect': []}
        for index, (wav, cue) in enumerate(prepared, 1):
            inputs += ['-i', wav]
            fade = min(.005 if cue['role'] == 'dialogue' else .15, cue['duration'] / 4)
            label = f'cue{index}'
            filters.append(f'[{index}:a]asetpts=PTS-STARTPTS,volume={cue["gain_db"]}dB,'
                f'afade=t=in:st=0:d={fade},afade=t=out:st={cue["duration"] - fade}:d={fade},'
                f'adelay={cue["start_sample"]}S:all=1[{label}]')
            buses['background' if cue['role'] in {'music', 'ambience'} else cue['role']].append(label)
        if keep_source_audio and source_audio:
            offset = float(source_audio.get('start_time') or 0) - float(video.get('start_time') or 0)
            fade = min(.15, duration / 4)
            filters.append(f'[0:a:0]aresample={RATE},atrim=start={max(0, -offset)},asetpts=PTS-STARTPTS,'
                f'adelay={round(max(0, offset) * RATE)}S:all=1,afade=t=in:st=0:d={fade},'
                f'afade=t=out:st={duration - fade}:d={fade}[source]')
            buses['background'].append('source')
        mixed = []
        for bus, labels in buses.items():
            if labels:
                filters.append(''.join(f'[{label}]' for label in labels) +
                    f'amix=inputs={len(labels)}:duration=longest:normalize=0,'
                    f'apad=whole_len={total_samples},atrim=end_sample={total_samples}[{bus}]')
                mixed.append(bus)
        ducked = 'dialogue' in mixed and 'background' in mixed
        if ducked:
            filters += ['[dialogue]asplit=2[voice][side]',
                '[background][side]sidechaincompress=threshold=0.0158:ratio=8:attack=10:release=250[ducked]']
            mixed = [('voice' if label == 'dialogue' else 'ducked' if label == 'background' else label)
                     for label in mixed]
        filters.append(''.join(f'[{label}]' for label in mixed) +
            f'amix=inputs={len(mixed)}:duration=longest:normalize=0,aresample={RATE},'
            f'apad=whole_len={total_samples},atrim=end_sample={total_samples},asetpts=N/SR/TB[mix]')
        graph = temporary / 'filters.txt'
        graph.write_text(';\n'.join(filters), encoding='utf8')
        premix = temporary / 'premix.wav'
        await run_command([binary('ffmpeg'), '-v', 'error', *inputs, '-filter_complex_script', graph,
            '-map', '[mix]', '-ar', RATE, '-ac', '2', '-c:a', 'pcm_f32le', premix])
        measured, _, _ = await _loudness(premix)
        if not all(math.isfinite(value) for value in measured.values()):
            raise ValueError('混音结果为静音或响度不可测，未注册为成功')
        normalization = ('loudnorm=I=-16:TP=-1.5:LRA=11:linear=true:'
            f'measured_I={measured["input_i"]}:measured_TP={measured["input_tp"]}:'
            f'measured_LRA={measured["input_lra"]}:measured_thresh={measured["input_thresh"]}:'
            f'offset={measured["target_offset"]},aresample={RATE},apad=whole_len={total_samples},'
            f'atrim=end_sample={total_samples},asetpts=N/SR/TB')
        webm = video['codec_name'] == 'vp8'
        output = temporary / ('sound.webm' if webm else 'sound.mp4')
        await run_command([binary('ffmpeg'), '-v', 'error', '-i', source, '-i', premix,
            '-map', '0:v:0', '-map', '1:a:0', '-c:v', 'copy', '-af', normalization,
            '-c:a', 'libopus' if webm else 'aac', '-b:a', '192k', '-ar', RATE, '-ac', '2',
            '-t', duration, *(['-avoid_negative_ts', 'disabled'] if webm else ['-movflags', '+faststart']), output])
        final_info, final_video, final_duration, final_frames, final_fps = await _video_facts(output)
        if (final_frames != frames or final_fps != fps or final_video['codec_name'] != video['codec_name']
                or any(final_video.get(key) != video.get(key) for key in ('width', 'height'))
                or abs(final_duration - duration) > max(1 / RATE, .001)):
            raise RuntimeError('混音改变了视频帧数、尺寸、编码、帧率或时长')
        if await _video_hash(source) != await _video_hash(output):
            raise RuntimeError('混音的视频码流与原始素材不一致')
        await run_command([binary('ffmpeg'), '-v', 'error', '-xerror', '-err_detect', 'explode',
            '-i', output, '-map', '0:v:0', '-map', '0:a:0', '-f', 'null', '-'])
        loudness, silences, trailing_silence = await _loudness(output)
        if not all(math.isfinite(value) for value in loudness.values()):
            raise RuntimeError('最终音轨为静音或实际响度无效')
        if loudness['input_tp'] > -.8 or abs(loudness['input_i'] + 16) > 1.5:
            raise RuntimeError('最终音轨的实际响度或 true peak 不符合交付范围')
        audio_stats = await _decode_audio(output, temporary / 'verified.wav')
        if abs(audio_stats['samples'] - total_samples) > 1024:
            raise RuntimeError('最终音轨实际样本长度被截断或超出编码填充范围')
        if trailing_silence is not None:
            silences.append({'start': trailing_silence, 'end': duration})
        validation = {'full_decode': True, 'video_bitstream_preserved': True, 'frames': frames,
            'duration': final_duration, 'fps': str(fps), 'audio_sample_rate': RATE,
            'container_duration': float(final_info['format']['duration']),
            'audio_samples': audio_stats['samples'], 'expected_audio_samples': total_samples,
            'encoder_padding_samples': audio_stats['samples'] - total_samples,
            'integrated_lufs': loudness['input_i'], 'true_peak_dbfs': loudness['input_tp'],
            'loudness_range_lu': loudness['input_lra'], 'silence_intervals': silences,
            'clipped_sample_ratio': audio_stats['clipped_sample_ratio'], 'background_ducked': ducked,
            'cue_truncated': False,
            'output_sha256': hashlib.sha256(output.read_bytes()).hexdigest(),
            'audio_codec': next(item['codec_name'] for item in final_info['streams'] if item['codec_type'] == 'audio')}
        timeline = {'revision': REVISION, 'fingerprint': fingerprint, 'video_asset_id': video_asset_id,
            'keep_source_audio': keep_source_audio, 'source_audio_present': source_audio is not None,
            'speaker_voices': voices, 'cues': [item for _, item in prepared]}
        return await register(store, output, name, {'sound_timeline': timeline, 'sound_validation': validation}, result_id)
