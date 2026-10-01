"""Synthetic PCM/codec fixtures only: no microphones or provider requests."""
from array import array
import hashlib
import io
import math
import os
from pathlib import Path
import stat
import subprocess
import wave

import pytest

from studio import enrollment_audio as audio


def wav_bytes(duration_ms=1000, *, amplitude=6000, silence=False, clipped=False, rate=24000):
    values = array('h', (0 if silence else (32767 if clipped else int(amplitude * math.sin(2 * math.pi * 440 * i / rate))) for i in range(rate * duration_ms // 1000)))
    output = io.BytesIO()
    with wave.open(output, 'wb') as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(values.tobytes())
    return output.getvalue()


def chunk(tmp_path, seq=0, *, data=None, mime='audio/wav', claimed_ms=15000):
    data = wav_bytes() if data is None else data
    path = tmp_path / f'chunk-{seq}.audio'
    path.write_bytes(data)
    return {'seq': seq, 'sha256': hashlib.sha256(data).hexdigest(), 'byte_count': len(data),
            'duration_ms': claimed_ms, 'mime': mime, 'path': str(path), 'role': 'contributor'}


@pytest.fixture
def decoder():
    if not audio.decoder_available():
        pytest.skip('FFmpeg is required for real codec validation; Docker installs it.')


def convert(data, container, codec):
    result = subprocess.run(['ffmpeg', '-v', 'error', '-f', 'wav', '-i', 'pipe:0', '-c:a', codec,
                             '-f', container, 'pipe:1'], input=data, capture_output=True, check=True)
    return result.stdout


def test_actual_duration_overrides_untrusted_claim(tmp_path, decoder):
    source = chunk(tmp_path, claimed_ms=15000)
    metrics = audio.validate_audio_chunk(Path(source['path']), mime=source['mime'], expected_sha256=source['sha256'])
    assert metrics['duration_ms'] == 1000
    assert metrics['active_ms'] == 1000
    assert metrics['usable_for_clone']
    assert metrics['speaker_verified'] is False


@pytest.mark.parametrize('data,reason', [(b'not a recording', 'invalid_audio'), (b'#EXTM3U\nhttps://example.invalid/remote.wav', 'invalid_audio')])
def test_invalid_bytes_and_playlist_are_not_audio(tmp_path, decoder, data, reason):
    source = chunk(tmp_path, data=data)
    with pytest.raises(audio.AudioValidationError) as error:
        audio.validate_audio_chunk(Path(source['path']), mime='audio/wav')
    assert error.value.code == reason


def test_missing_decoder_is_explicit(tmp_path, monkeypatch):
    source = chunk(tmp_path)
    monkeypatch.setattr(audio.shutil, 'which', lambda value: None)
    with pytest.raises(audio.DecoderUnavailable, match='FFmpeg'):
        audio.validate_audio_chunk(Path(source['path']), mime='audio/wav')


def test_silence_and_clipping_are_excluded(tmp_path, decoder):
    for seq, kwargs, reason in [(0, {'silence': True}, 'insufficient_audible_audio'), (1, {'clipped': True}, 'clipped_audio')]:
        source = chunk(tmp_path, seq, data=wav_bytes(**kwargs))
        metrics = audio.validate_audio_chunk(Path(source['path']), mime='audio/wav')
        assert not metrics['usable_for_clone']
        assert metrics['rejection_reason'] == reason


def test_checksum_size_and_symlinks_are_enforced(tmp_path, decoder):
    source = chunk(tmp_path)
    with pytest.raises(audio.AudioValidationError, match='checksum'):
        audio.validate_audio_chunk(Path(source['path']), mime='audio/wav', expected_sha256='0' * 64)
    source = chunk(tmp_path, 1, data=b'x' * (audio.CHUNK_MAX_BYTES + 1))
    with pytest.raises(audio.AudioValidationError) as error:
        audio.validate_audio_chunk(Path(source['path']), mime='audio/wav')
    assert error.value.code == 'audio_too_large'
    link = tmp_path / 'linked.wav'
    link.symlink_to(tmp_path / 'chunk-0.audio')
    with pytest.raises(audio.AudioValidationError) as error:
        audio.validate_audio_chunk(link, mime='audio/wav')
    assert error.value.code == 'audio_unavailable'


def test_fifo_is_rejected_without_blocking(tmp_path, decoder):
    path = tmp_path / 'fifo'
    os.mkfifo(path)
    with pytest.raises(audio.AudioValidationError) as error:
        audio.validate_audio_chunk(path, mime='audio/wav')
    assert error.value.code == 'invalid_audio_file'


def test_manifest_requires_final_contiguous_sequence(tmp_path):
    first, second = chunk(tmp_path, 0), chunk(tmp_path, 1)
    manifest = audio.build_capture_manifest([second, first], final_seq=1)
    assert manifest['last_seq'] == 1
    assert len(manifest['digest']) == 64
    assert 'path' not in str(manifest)
    for chunks, final_seq in [([first], 1), ([second], 1), ([first, second], 0), ([first, first], 1)]:
        with pytest.raises(audio.AudioValidationError) as error:
            audio.build_capture_manifest(chunks, final_seq=final_seq)
        assert error.value.code == 'audio_sequence_gap'


def test_interviewer_audio_is_never_selected(tmp_path):
    source = chunk(tmp_path)
    source['role'] = 'interviewer'
    with pytest.raises(audio.AudioValidationError) as error:
        audio.prepare_clone_sample([source], tmp_path / 'output', min_active_ms=500)
    assert error.value.code == 'invalid_speaker_role'


def test_decode_then_join_independent_webm_segments(tmp_path, decoder):
    sources = [chunk(tmp_path, index, data=convert(wav_bytes(), 'webm', 'libopus'), mime='audio/webm') for index in range(3)]
    files, manifest = audio.prepare_clone_sample(sources, tmp_path / 'samples', min_active_ms=2000)
    assert len(files) == 1
    assert manifest['total_ms'] == 3000
    assert manifest['active_ms'] == 3000
    assert [item['seq'] for item in manifest['chunks']] == [0, 1, 2]
    path = Path(files[0]['path'])
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert manifest['normalized_sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
    with wave.open(str(path), 'rb') as reader:
        assert reader.getnframes() == 72000
        assert reader.getframerate() == 24000
        assert reader.getnchannels() == 1
    assert manifest['speaker_verified'] is False
    assert manifest['human_listening_required'] is True


def test_ogg_is_decoded(tmp_path, decoder):
    source = chunk(tmp_path, data=convert(wav_bytes(), 'ogg', 'libopus'), mime='audio/ogg')
    metrics = audio.validate_audio_chunk(Path(source['path']), mime=source['mime'])
    assert metrics['duration_ms'] == 1000


def test_delayed_recorder_blob_over_80kib_is_decoded_and_selected(tmp_path, decoder):
    # A delayed MediaRecorder stop can yield more than a nominal 3 s segment.
    # Keep the browser's 128 kbps encoding rate and validate actual duration.
    result = subprocess.run(
        ['ffmpeg', '-v', 'error', '-f', 'wav', '-i', 'pipe:0',
         '-c:a', 'libopus', '-b:a', '128k', '-vbr', 'off', '-f', 'webm', 'pipe:1'],
        input=wav_bytes(8000), capture_output=True, check=True,
    )
    data = result.stdout
    assert 80 * 1024 < len(data) <= audio.CHUNK_MAX_BYTES
    source = chunk(tmp_path, data=data, mime='audio/webm', claimed_ms=3000)
    metrics = audio.validate_audio_chunk(
        Path(source['path']), mime=source['mime'], expected_sha256=source['sha256'],
    )
    assert metrics['duration_ms'] == 8000
    assert metrics['usable_for_clone']
    files, manifest = audio.prepare_clone_sample(
        [source], tmp_path / 'samples', min_active_ms=7000,
    )
    assert len(files) == 1
    assert manifest['total_ms'] == 8000
    assert manifest['chunks'][0]['sha256'] == source['sha256']


def test_sample_is_deterministic_and_bad_segments_excluded(tmp_path, decoder):
    sources = [chunk(tmp_path), chunk(tmp_path, 1, data=b'invalid'), chunk(tmp_path, 2, data=wav_bytes(silence=True)), chunk(tmp_path, 3)]
    files, manifest = audio.prepare_clone_sample(sources, tmp_path / 'samples', min_active_ms=1500)
    next_files, next_manifest = audio.prepare_clone_sample(sources, tmp_path / 'samples', min_active_ms=1500)
    assert manifest == next_manifest
    assert files[0]['sha256'] == next_files[0]['sha256']
    assert files[0]['path'] != next_files[0]['path']
    assert [item['seq'] for item in manifest['chunks']] == [0, 3]
    assert [item['seq'] for item in manifest['rejected']] == [1, 2]


def test_insufficient_or_changed_audio_cannot_create_sample(tmp_path, decoder):
    source = chunk(tmp_path)
    output = tmp_path / 'samples'
    with pytest.raises(audio.AudioValidationError) as error:
        audio.prepare_clone_sample([source], output, min_active_ms=2000)
    assert error.value.code == 'insufficient_audio'
    assert not output.exists()
    Path(source['path']).write_bytes(wav_bytes(amplitude=7000))
    with pytest.raises(audio.AudioValidationError) as error:
        audio.prepare_clone_sample([source], output, min_active_ms=500)
    assert error.value.code == 'insufficient_audio'
    assert not output.exists()


def test_overlong_small_compressed_file_is_rejected(tmp_path, decoder):
    data = convert(wav_bytes(16000, silence=True), 'ogg', 'libopus')
    assert len(data) < audio.CHUNK_MAX_BYTES
    source = chunk(tmp_path, data=data, mime='audio/ogg')
    with pytest.raises(audio.AudioValidationError) as error:
        audio.validate_audio_chunk(Path(source['path']), mime='audio/ogg')
    assert error.value.code == 'audio_too_long'


def test_preview_must_be_decodable_audible_fresh_bytes(decoder):
    data = convert(wav_bytes(), 'mp3', 'libmp3lame')
    assert audio.validate_synthesized_audio(data)['active_ms'] >= 900
    with pytest.raises(audio.AudioValidationError):
        audio.validate_synthesized_audio(b'ID3' + b'x' * 1500)
    with pytest.raises(audio.AudioValidationError) as error:
        audio.validate_synthesized_audio(wav_bytes(silence=True), mime='audio/wav')
    assert error.value.code == 'silent_preview_audio'


def test_decoder_timeout_is_safe(tmp_path, monkeypatch):
    source = chunk(tmp_path)
    monkeypatch.setattr(audio.shutil, 'which', lambda value: '/synthetic/ffmpeg')
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired('synthetic decoder', 8)
    monkeypatch.setattr(audio.subprocess, 'run', timeout)
    with pytest.raises(audio.AudioValidationError) as error:
        audio.validate_audio_chunk(Path(source['path']), mime='audio/wav')
    assert error.value.code == 'audio_decode_timeout'


def patterned_audio(duration_ms, active_ranges, *, quiet_amplitude=0):
    """Synthetic unchanged PCM with known audible and low-energy intervals."""
    values = array('h', (
        int((6000 if any(start * 24 <= i < end * 24 for start, end in active_ranges) else quiet_amplitude)
            * math.sin(2 * math.pi * 440 * i / 24000))
        for i in range(duration_ms * 24)
    ))
    pcm = values.tobytes()
    output = io.BytesIO()
    with wave.open(output, 'wb') as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(24000)
        writer.writeframes(pcm)
    return output.getvalue(), pcm


@pytest.mark.parametrize('layout', ['uniform_sparse', 'later_dense', 'short_utterances'])
def test_long_capture_selects_actual_activity_without_filling_with_silence(tmp_path, decoder, layout):
    # 400 x 3 s is a 20-minute capture. Neither elapsed time nor reported time
    # establishes readiness; actual decoded activity must still reach 60 s.
    active_end = 800 if layout == 'short_utterances' else 1200
    sparse, _ = patterned_audio(3000, [(0, active_end)])
    first = chunk(tmp_path, 0, data=sparse, claimed_ms=3000)
    dense = chunk(tmp_path, 1, data=wav_bytes(3000), claimed_ms=3000)
    sources = [dict(dense if layout == 'later_dense' and index >= 200 else first, seq=index) for index in range(400)]
    files, manifest = audio.prepare_clone_sample(sources, tmp_path / 'samples')
    assert manifest['active_ms'] >= 60000
    assert manifest['active_ms'] <= manifest['total_ms'] <= 125000
    assert manifest['diagnostics']['inspected_chunks'] <= 128
    assert manifest['diagnostics']['saved_reported_ms'] == 1200000
    assert manifest['selection'] == 'decoded_ranked_context_spans_v3'
    assert [entry['seq'] for entry in manifest['chunks']] == sorted(entry['seq'] for entry in manifest['chunks'])
    for entry in manifest['chunks']:
        assert entry['sha256'] == sources[entry['seq']]['sha256']
        assert entry['decoded_source']['duration_ms'] == 3000
        assert entry['included_spans']
    if layout == 'short_utterances':
        assert all(not entry['decoded_source']['usable_for_clone'] for entry in manifest['chunks'])
    with wave.open(files[0]['path'], 'rb') as reader:
        assert reader.getnframes() <= 125 * 24000


def test_source_spans_preserve_pcm_short_pauses_and_context_exactly(tmp_path, decoder):
    data, pcm = patterned_audio(4000, [(600, 1000), (1300, 1600), (2600, 3200)], quiet_amplitude=100)
    source = chunk(tmp_path, data=data, claimed_ms=4000)
    files, manifest = audio.prepare_clone_sample([source], tmp_path / 'samples', min_active_ms=1000)
    entry = manifest['chunks'][0]
    # 300 ms natural pause is preserved; the 1000 ms low-energy gap is reduced
    # to 200 ms contextual padding on either side. No sample values are changed.
    expected_spans = [(400 * 24, 1800 * 24), (2400 * 24, 3400 * 24)]
    assert [(span['start_sample'], span['end_sample']) for span in entry['included_spans']] == expected_spans
    expected_pcm = b''.join(pcm[start * 2:end * 2] for start, end in expected_spans)
    with wave.open(files[0]['path'], 'rb') as reader:
        actual_pcm = reader.readframes(reader.getnframes())
    assert actual_pcm == expected_pcm
    assert Path(source['path']).read_bytes() == data
    assert entry['decoded_source']['duration_ms'] == 4000
    assert entry['duration_ms'] == 2400
    assert entry['active_ms'] == 1300
    for span in entry['included_spans']:
        assert actual_pcm[span['output_start_sample'] * 2:span['output_end_sample'] * 2] == pcm[span['start_sample'] * 2:span['end_sample'] * 2]


def test_retained_clipping_is_rejected_even_if_full_source_passes(tmp_path, decoder):
    data, pcm = patterned_audio(3000, [(1000, 1800)])
    values = array('h')
    values.frombytes(pcm)
    # 250 clipped samples / 72000 < 0.5%; after edge trimming, 250 / 28800 > 0.5%.
    values[1100 * 24:1100 * 24 + 250] = array('h', [32767] * 250)
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as writer:
        writer.setnchannels(1); writer.setsampwidth(2); writer.setframerate(24000)
        writer.writeframes(values.tobytes())
    source = chunk(tmp_path, data=buffer.getvalue())
    with pytest.raises(audio.AudioValidationError) as error:
        audio.prepare_clone_sample([source], tmp_path / 'samples', min_active_ms=500)
    assert error.value.code == 'insufficient_audio'
    assert error.value.details['rejected_reasons'] == {'clipped_audio': 1}
    assert not (tmp_path / 'samples').exists()


def test_insufficiency_has_bounded_private_free_diagnostics(tmp_path, decoder):
    sources = [chunk(tmp_path, data=wav_bytes(silence=True), claimed_ms=1000), chunk(tmp_path, 1, data=b'invalid', claimed_ms=1000)]
    with pytest.raises(audio.AudioValidationError) as error:
        audio.prepare_clone_sample(sources, tmp_path / 'samples', min_active_ms=500)
    assert error.value.details == {
        'saved_reported_ms': 2000, 'captured_chunks': 2, 'inspected_chunks': 2, 'decoder_invocations': 2,
        'decoded_source_ms': 1000, 'decoded_active_ms': 0,
        'selected_active_ms': 0, 'selected_duration_ms': 0, 'minimum_active_ms': 500, 'minimum_sample_ms': 500,
        'rejected_chunks': 2, 'rejected_reasons': {'insufficient_audible_audio': 1, 'invalid_audio': 1},
        'partial': False, 'insufficiency': 'sample_and_activity',
    }
    assert str(tmp_path) not in str(error.value.details)


def test_maximum_candidate_count_and_output_limits_survive_trimming(tmp_path, decoder, monkeypatch):
    data, _ = patterned_audio(3000, [(1000, 1800)])
    source = chunk(tmp_path, data=data, claimed_ms=3000)
    sources = [dict(source, seq=index) for index in range(400)]
    real_decode = audio._decode
    calls = []
    def counted(*args, **kwargs):
        calls.append(kwargs.get('timeout_seconds'))
        return real_decode(*args, **kwargs)
    monkeypatch.setattr(audio, '_decode', counted)
    files, manifest = audio.prepare_clone_sample(sources, tmp_path / 'samples', min_active_ms=500, max_sample_ms=2500)
    assert len(calls) <= 256
    assert manifest['diagnostics']['inspected_chunks'] <= 128
    assert manifest['diagnostics']['decoder_invocations'] == len(calls)
    assert all(0 < timeout <= 60 for timeout in calls)
    assert manifest['total_ms'] == 2400
    assert manifest['active_ms'] == 1600
    with wave.open(files[0]['path'], 'rb') as reader:
        assert reader.getnframes() == 2400 * 24


def test_total_deadline_restricts_each_decoder_and_returns_no_file(tmp_path, monkeypatch):
    source = chunk(tmp_path)
    clock = {'now': 100.0}
    monkeypatch.setattr(audio.time, 'monotonic', lambda: clock['now'])
    original_read = audio._read_private_file
    def slow_read(*args, **kwargs):
        clock['now'] = 159.5
        return original_read(*args, **kwargs)
    monkeypatch.setattr(audio, '_read_private_file', slow_read)
    seen = []
    def decoder(*args, **kwargs):
        seen.append(kwargs['timeout_seconds'])
        clock['now'] = 161.0
        raise audio.AudioValidationError('audio_decode_timeout', 'Synthetic timeout')
    monkeypatch.setattr(audio, '_decode', decoder)
    with pytest.raises(audio.AudioValidationError) as error:
        audio.prepare_clone_sample([source, dict(source, seq=1)], tmp_path / 'samples', min_active_ms=500)
    assert seen == [0.5]
    assert error.value.code == 'audio_prepare_timeout'
    assert error.value.details['inspected_chunks'] == 1
    assert not (tmp_path / 'samples').exists()


WORD_GAP_RANGES = [(100, 300), (600, 800), (1100, 1300), (1600, 1800), (2120, 2300), (2620, 2800)]


def test_uniform_word_pauses_use_separate_runtime_and_local_activity_policy(tmp_path, decoder):
    data, _ = patterned_audio(3000, WORD_GAP_RANGES)
    source = chunk(tmp_path, data=data, claimed_ms=3000)
    sources = [dict(source, seq=index) for index in range(400)]
    files, manifest = audio.prepare_clone_sample(
        sources, tmp_path / 'samples', min_active_ms=30000, min_sample_ms=60000,
    )
    assert manifest['total_ms'] == 123000
    assert manifest['active_ms'] == 47560
    assert manifest['diagnostics']['minimum_sample_ms'] == 60000
    assert manifest['diagnostics']['minimum_active_ms'] == 30000
    assert manifest['diagnostics']['decoder_invocations'] <= 256
    assert manifest['diagnostics']['inspected_chunks'] == 128
    assert 'not speech duration' in manifest['screening_limits']
    with wave.open(files[0]['path'], 'rb') as reader:
        assert reader.getnframes() == 123000 * 24


def test_density_ranking_replaces_early_word_pauses_with_later_better_candidates(tmp_path, decoder):
    data, _ = patterned_audio(3000, WORD_GAP_RANGES)
    first = chunk(tmp_path, data=data, claimed_ms=3000)
    dense = chunk(tmp_path, 1, data=wav_bytes(3000), claimed_ms=3000)
    sources = [dict(first if index < 200 else dense, seq=index) for index in range(400)]
    # The legacy 60 s ACTIVITY default must pass when the bounded candidate set
    # already contains denser audio; no threshold reduction is involved here.
    files, manifest = audio.prepare_clone_sample(sources, tmp_path / 'samples')
    assert manifest['total_ms'] == 123000
    assert manifest['active_ms'] == 123000
    assert all(entry['seq'] >= 200 for entry in manifest['chunks'])
    assert [entry['seq'] for entry in manifest['chunks']] == sorted(entry['seq'] for entry in manifest['chunks'])
    assert manifest['diagnostics']['inspected_chunks'] == 128
    assert manifest['diagnostics']['decoder_invocations'] == 169
    with wave.open(files[0]['path'], 'rb') as reader:
        assert reader.getnframes() == 123000 * 24


def test_incomplete_analysis_does_not_claim_entire_capture_insufficient(tmp_path, decoder):
    source = chunk(tmp_path, data=wav_bytes(3000, silence=True), claimed_ms=3000)
    sources = [dict(source, seq=index) for index in range(400)]
    with pytest.raises(audio.AudioValidationError) as error:
        audio.prepare_clone_sample(sources, tmp_path / 'samples', min_active_ms=30000, min_sample_ms=60000)
    assert error.value.code == 'audio_analysis_incomplete'
    assert error.value.details['partial'] is True
    assert error.value.details['inspected_chunks'] == 128
    assert error.value.details['insufficiency'] == 'sample_and_activity'
    assert error.value.details['minimum_sample_ms'] == 60000
    assert not (tmp_path / 'samples').exists()


def test_sample_runtime_gate_is_separate_from_activity(tmp_path, decoder):
    source = chunk(tmp_path, data=wav_bytes(1000), claimed_ms=1000)
    with pytest.raises(audio.AudioValidationError) as error:
        audio.prepare_clone_sample([source], tmp_path / 'samples', min_active_ms=500, min_sample_ms=2000)
    assert error.value.code == 'insufficient_audio'
    assert error.value.details['insufficiency'] == 'sample_duration'
    assert error.value.details['selected_active_ms'] == 1000
    assert not (tmp_path / 'samples').exists()


def test_second_pass_rechecks_checksum_before_using_ranked_metadata(tmp_path, decoder, monkeypatch):
    source = chunk(tmp_path)
    actual_read = audio._read_private_file
    reads = []
    def changed_between_passes(path, maximum):
        reads.append(path)
        if len(reads) == 2:
            Path(path).write_bytes(wav_bytes(amplitude=7000))
        return actual_read(path, maximum)
    monkeypatch.setattr(audio, '_read_private_file', changed_between_passes)
    with pytest.raises(audio.AudioValidationError) as error:
        audio.prepare_clone_sample([source], tmp_path / 'samples', min_active_ms=500)
    assert error.value.code == 'audio_checksum_mismatch'
    assert len(reads) == 2
    assert not (tmp_path / 'samples').exists()


def test_second_pass_shares_original_deadline_and_cleans_output(tmp_path, decoder, monkeypatch):
    source = chunk(tmp_path)
    clock = {'now': 100.0}
    monkeypatch.setattr(audio.time, 'monotonic', lambda: clock['now'])
    actual_decode = audio._decode
    budgets = []
    def slow_second_pass(*args, **kwargs):
        budgets.append(kwargs['timeout_seconds'])
        result = actual_decode(*args, **kwargs)
        clock['now'] = 159.0 if len(budgets) == 1 else 161.0
        return result
    monkeypatch.setattr(audio, '_decode', slow_second_pass)
    with pytest.raises(audio.AudioValidationError) as error:
        audio.prepare_clone_sample([source], tmp_path / 'samples', min_active_ms=500)
    assert budgets == [60.0, 1.0]
    assert error.value.code == 'audio_prepare_timeout'
    assert error.value.details['decoder_invocations'] == 2
    assert not (tmp_path / 'samples').exists()
