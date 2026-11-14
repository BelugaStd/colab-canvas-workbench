import array
import hashlib
import math
import shutil
import sys
import uuid
import wave

import pytest
from fastapi import HTTPException

from backend import agent_sound
from backend.agent_media import register, run_command
from backend.assets import asset_path
from backend.store import Store

pytestmark = pytest.mark.skipif(not shutil.which('ffmpeg'), reason='ffmpeg required')


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / 'data')
    yield value
    value.close()


async def video(store, *, audio=False, duration=3, webm=False):
    path = store.directory / ('source.webm' if webm else 'source.mp4')
    args = ['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=64x48:r=24']
    if audio:
        args += ['-f', 'lavfi', '-i', 'sine=frequency=80:sample_rate=48000']
    await run_command([*args, '-t', duration, '-c:v', 'libvpx' if webm else 'libx264',
                       '-pix_fmt', 'yuv420p', '-c:a', 'libopus' if webm else 'aac', path])
    return await register(store, path, 'Original video')


async def tone(store, *, duration=.5, frequency=500, voice='Serena', text='Test line', silent=False):
    path = store.directory / (uuid.uuid4().hex + '.wav')
    count = round(duration * 48000)
    values = array.array('h', (0 if silent else round(6000 * math.sin(2 * math.pi * frequency * i / 48000))
                              for i in range(count)))
    if sys.byteorder != 'little':
        values.byteswap()
    with wave.open(str(path), 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(48000)
        stream.writeframes(values.tobytes())
    metadata = {'agent_speech': {'speaker': voice, 'text': text, 'samples': count,
                                'sample_rate': 48000, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}}
    return await register(store, path, 'Original speech', metadata)


async def decoded_samples(store, asset):
    path, _ = asset_path(store, asset['id'])
    raw = await run_command(['ffmpeg', '-v', 'error', '-i', path, '-map', '0:a:0',
                             '-ar', '48000', '-ac', '1', '-f', 's16le', '-'])
    values = array.array('h')
    values.frombytes(raw)
    if sys.byteorder != 'little':
        values.byteswap()
    return values


def rms(values, start, end):
    selected = values[round(start * 48000):round(end * 48000)]
    return math.sqrt(sum(v * v for v in selected) / max(1, len(selected)))


def frequency_level(values, start, end, frequency):
    selected = values[round(start * 48000):round(end * 48000)]
    sine = sum(value * math.sin(2 * math.pi * frequency * i / 48000) for i, value in enumerate(selected))
    cosine = sum(value * math.cos(2 * math.pi * frequency * i / 48000) for i, value in enumerate(selected))
    return math.hypot(sine, cosine) / len(selected)


async def test_mix_preserves_video_and_originals_and_records_actual_sample_timeline(store):
    source, speech = await video(store), await tone(store)
    source_path, _ = asset_path(store, source['id'])
    speech_path, _ = asset_path(store, speech['id'])
    originals = [hashlib.sha256(p.read_bytes()).hexdigest() for p in (source_path, speech_path)]
    result = await agent_sound.mix(store, source['id'], [
        {'asset_id': speech['id'], 'start': .500007, 'role': 'dialogue', 'speaker': 'hero'}], 'Mixed')
    cue = result['sound_timeline']['cues'][0]
    assert cue['start_sample'] == 24000 and cue['samples'] == 24000
    assert cue['duration'] == .5 and cue['voice'] == 'Serena' and cue['text'] == 'Test line'
    validation = result['sound_validation']
    assert validation['full_decode'] is True and validation['video_bitstream_preserved'] is True
    assert validation['frames'] == 72 and abs(validation['duration'] - 3) < .05
    assert validation['true_peak_dbfs'] <= -.8 and -17.5 < validation['integrated_lufs'] < -14.5
    values = await decoded_samples(store, result)
    assert rms(values, .05, .35) < 3 and rms(values, .55, .9) > 500
    assert [hashlib.sha256(p.read_bytes()).hexdigest() for p in (source_path, speech_path)] == originals


@pytest.mark.parametrize('case', ['outside', 'overlap', 'voice', 'transcript', 'gain', 'url', 'extra'])
async def test_invalid_timeline_never_registers_partial_film(store, case):
    source, first = await video(store), await tone(store)
    cues = [{'asset_id': first['id'], 'start': .5, 'role': 'dialogue', 'speaker': 'hero'}]
    if case == 'outside':
        cues[0]['start'] = 2.6
    elif case == 'overlap':
        cues.append({**cues[0], 'start': .7})
    elif case == 'voice':
        second = await tone(store, voice='Vivian')
        cues.append({**cues[0], 'asset_id': second['id'], 'start': 1.5})
    elif case == 'transcript':
        cues[0]['text'] = 'Not the recorded words'
    elif case == 'gain':
        cues[0]['gain_db'] = 80
    elif case == 'url':
        cues[0]['asset_id'] = 'https://invalid.example/speech.wav'
    else:
        cues[0]['duration'] = .1
    before = len(store.all('assets'))
    with pytest.raises((ValueError, HTTPException)):
        await agent_sound.mix(store, source['id'], cues, 'Invalid')
    assert len(store.all('assets')) == before


async def test_explicit_interruption_and_idempotence_with_conflict_detection(store):
    source, first, second = await video(store), await tone(store), await tone(store, voice='Vivian')
    cues = [{'asset_id': first['id'], 'start': .4, 'role': 'dialogue', 'speaker': 'hero'},
            {'asset_id': second['id'], 'start': .7, 'role': 'dialogue', 'speaker': 'friend', 'allow_overlap': True}]
    key = uuid.uuid4().hex
    result = await agent_sound.mix(store, source['id'], cues, 'Interruption', key)
    assert await agent_sound.mix(store, source['id'], cues, 'Interruption', key) == result
    assert result['sound_timeline']['speaker_voices'] == {'hero': 'Serena', 'friend': 'Vivian'}
    with pytest.raises(ValueError, match='不同|不一致'):
        await agent_sound.mix(store, source['id'], [{**cues[0], 'start': .1}], 'Interruption', key)
    assert len([a for a in store.all('assets') if a['id'] == key]) == 1
    path, _ = asset_path(store, key)
    path.write_bytes(path.read_bytes()[:-100])
    with pytest.raises(RuntimeError, match='损坏|校验'):
        await agent_sound.mix(store, source['id'], cues, 'Interruption', key)


async def test_source_sound_can_be_disabled_and_background_is_ducked_by_dialogue(store):
    source, speech = await video(store, audio=True), await tone(store, duration=1.0)
    cues = [{'asset_id': speech['id'], 'start': 1, 'role': 'dialogue', 'speaker': 'hero'}]
    keep = await agent_sound.mix(store, source['id'], cues, 'Keep source')
    muted = await agent_sound.mix(store, source['id'], cues, 'Mute source', keep_source_audio=False)
    kept_samples, muted_samples = await decoded_samples(store, keep), await decoded_samples(store, muted)
    assert rms(kept_samples, .3, .7) > 100
    assert rms(kept_samples, 2.4, 2.8) > 100
    assert rms(muted_samples, .3, .7) < 3
    before = frequency_level(kept_samples, .3, .7, 80)
    during = frequency_level(kept_samples, 1.3, 1.7, 80)
    assert during < before * .65
    assert keep['sound_timeline']['keep_source_audio'] is True
    assert muted['sound_timeline']['keep_source_audio'] is False
    assert keep['sound_validation']['background_ducked'] is True


async def test_music_ambience_effects_accept_local_audio_without_faking_speech(store):
    source, sound = await video(store), await tone(store)
    metadata = store.get('assets', sound['id'])
    metadata.pop('agent_speech')
    store.put('assets', sound['id'], metadata)
    result = await agent_sound.mix(store, source['id'], [
        {'asset_id': sound['id'], 'start': 0, 'role': 'ambience', 'gain_db': -12},
        {'asset_id': sound['id'], 'start': 1, 'role': 'music', 'gain_db': -9},
        {'asset_id': sound['id'], 'start': 2, 'role': 'effect', 'gain_db': -3}], 'Soundscape')
    assert result['sound_timeline']['speaker_voices'] == {}
    assert len(result['sound_timeline']['cues']) == 3
    with pytest.raises(ValueError, match='配音|声音|speech'):
        await agent_sound.mix(store, source['id'], [
            {'asset_id': sound['id'], 'start': 0, 'role': 'dialogue'}], 'Unknown voice')


async def test_silence_and_modified_speech_are_visible_errors(store):
    source, speech = await video(store), await tone(store, silent=True)
    cues = [{'asset_id': speech['id'], 'start': 0, 'role': 'dialogue'}]
    with pytest.raises(ValueError, match='静音'):
        await agent_sound.mix(store, source['id'], cues, 'Silence')
    path, _ = asset_path(store, speech['id'])
    path.write_bytes(path.read_bytes()[:-200])
    with pytest.raises((ValueError, RuntimeError), match='完整|损坏|一致|截断|媒体'):
        await agent_sound.mix(store, source['id'], cues, 'Truncated')


async def test_invalid_result_identifier_cannot_escape_asset_directory(store):
    source, speech = await video(store), await tone(store)
    with pytest.raises(ValueError):
        await agent_sound.mix(store, source['id'], [
            {'asset_id': speech['id'], 'start': 0, 'role': 'dialogue'}], 'Escape', '../escaped')


async def test_webm_video_copy_uses_video_packet_duration_instead_of_audio_padding(store):
    source, speech = await video(store, webm=True), await tone(store)
    result = await agent_sound.mix(store, source['id'], [
        {'asset_id': speech['id'], 'start': .5, 'role': 'dialogue'}], 'WebM sound')
    assert result['media_type'] == 'video/webm'
    assert result['sound_validation']['frames'] == 72
    assert abs(result['sound_validation']['duration'] - 3) < .001
    assert result['sound_validation']['video_bitstream_preserved'] is True


async def test_three_minute_dialogue_timeline_with_reused_voices_preserves_all_utterances(store):
    source = await video(store, duration=180)
    first = await tone(store, duration=2.6)
    second = await tone(store, duration=2.6, voice='Vivian')
    cues = [{'asset_id': (first if index % 2 == 0 else second)['id'], 'start': index * 3,
             'role': 'dialogue', 'speaker': 'hero' if index % 2 == 0 else 'friend'} for index in range(60)]
    result = await agent_sound.mix(store, source['id'], cues, 'Three minute dialogue')
    assert result['sound_validation']['frames'] == 4320
    assert result['sound_validation']['duration'] == 180
    assert len(result['sound_timeline']['cues']) == 60
    assert sum(cue['duration'] for cue in result['sound_timeline']['cues']) == pytest.approx(156)
    values = await decoded_samples(store, result)
    assert rms(values, 177.2, 179.3) > 100
