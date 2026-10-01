"""Bounded local decoding and reproducible enrollment samples.

MediaRecorder is restarted for each upload, so each chunk is an independent
container. Decode these separately; concatenating WebM bytes loses segments.
Energy screening is NOT speech detection, speaker verification, or an echo test.
A contributor and a human listener must still approve the resulting voice.
"""
from __future__ import annotations

from array import array
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import wave

SAMPLE_RATE = 24000
CHUNK_MAX_BYTES = 256 * 1024
MAX_SOURCE_CHUNKS = 10001
MAX_DECODE_CHUNKS = 128
DECODE_TIMEOUT_SECONDS = 8
PREPARE_TIMEOUT_SECONDS = 60
ACTIVITY_CONTEXT_MS = 200
FORMATS = {"audio/webm": "matroska,webm", "audio/ogg": "ogg", "audio/wav": "wav", "audio/x-wav": "wav", "audio/mpeg": "mp3"}


class AudioValidationError(ValueError):
    def __init__(self, code: str, message: str, *, details: dict | None = None):
        self.code = code
        self.details = details or {}
        super().__init__(message)


class DecoderUnavailable(AudioValidationError):
    def __init__(self):
        super().__init__("decoder_unavailable", "Audio validation is unavailable; the server needs FFmpeg.")


def decoder_available() -> bool:
    return shutil.which("ffmpeg") is not None


def _read_private_file(path: Path, maximum: int) -> bytes:
    """No symlink following, and bound reads even if a file changes after stat."""
    path = Path(path)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise AudioValidationError("invalid_audio_file", "The saved audio is not a regular file.")
            if info.st_size > maximum:
                raise AudioValidationError("audio_too_large", "The audio exceeds the validation size limit.")
            data = handle.read(maximum + 1)
    except OSError:
        raise AudioValidationError("audio_unavailable", "Saved audio is unavailable; record another sample.") from None
    if not data or len(data) > maximum:
        raise AudioValidationError("audio_too_large", "The audio is empty or exceeds the validation size limit.")
    return data


def _decode(data: bytes, mime: str, max_duration_ms: int, *, timeout_seconds: float = DECODE_TIMEOUT_SECONDS) -> bytes:
    executable = shutil.which("ffmpeg")
    if not executable:
        raise DecoderUnavailable()
    mime = mime.split(";", 1)[0].lower()
    if mime not in FORMATS:
        raise AudioValidationError("unsupported_audio", "This audio container is not supported.")
    # Disable file/network protocols: the only input is these already bounded
    # bytes. Output has a hard duration cap, with a sentinel tail to detect an
    # overlong source rather than silently accepting a truncated chunk.
    limit_ms = max_duration_ms + 250
    command = [
        executable, "-hide_banner", "-loglevel", "error", "-nostdin",
        "-threads", "1", "-max_alloc", "33554432", "-xerror",
        "-protocol_whitelist", "pipe", "-format_whitelist", FORMATS[mime],
        "-f", "matroska" if mime == "audio/webm" else FORMATS[mime],
        "-i", "pipe:0", "-map", "0:a:0", "-vn", "-sn", "-dn",
        "-t", str(limit_ms / 1000), "-ac", "1", "-ar", str(SAMPLE_RATE),
        "-af", f"aresample={SAMPLE_RATE},atrim=end_sample={SAMPLE_RATE * limit_ms // 1000},asetpts=N/SR/TB",
        "-threads", "1", "-f", "s16le", "pipe:1",
    ]
    try:
        result = subprocess.run(command, input=data, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=min(DECODE_TIMEOUT_SECONDS, timeout_seconds),
                                check=False)
    except subprocess.TimeoutExpired:
        raise AudioValidationError("audio_decode_timeout", "Audio validation timed out; record another sample.") from None
    except OSError:
        raise DecoderUnavailable() from None
    pcm = result.stdout
    if result.returncode != 0 or len(pcm) < SAMPLE_RATE // 10 or len(pcm) % 2:
        raise AudioValidationError("invalid_audio", "The recording could not be decoded as usable audio.")
    actual_ms = len(pcm) * 1000 / (SAMPLE_RATE * 2)
    if actual_ms > max_duration_ms + 100:
        raise AudioValidationError("audio_too_long", "The decoded recording exceeds the permitted duration.")
    return pcm


def _analyze_pcm(pcm: bytes, spans: list[tuple[int, int]] | None = None) -> tuple[dict, list[tuple[int, int]]]:
    samples = array("h")
    samples.frombytes(pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    window_size = SAMPLE_RATE // 50  # 20 ms windows, fixed -40 dBFS threshold.
    active_samples = 0
    active_windows = []
    clipped = 0
    square_sum = 0
    spans = [(0, len(samples))] if spans is None else spans
    sample_count = sum(end - start for start, end in spans)
    for span_start, span_end in spans:
        for start in range(span_start, span_end, window_size):
            window = samples[start:min(start + window_size, span_end)]
            energy = sum(value * value for value in window)
            square_sum += energy
            clipped += sum(abs(value) >= 32700 for value in window)
            if energy >= len(window) * (32768 * 0.01) ** 2:
                active_samples += len(window)
                active_windows.append((start, start + len(window)))
    duration_ms = round(sample_count * 1000 / SAMPLE_RATE)
    active_ms = round(active_samples * 1000 / SAMPLE_RATE)
    clip_fraction = clipped / sample_count
    rms = math.sqrt(square_sum / sample_count) / 32768
    reason = None
    if active_ms < max(150, duration_ms * 0.35):
        reason = "insufficient_audible_audio"
    elif clip_fraction > 0.005:
        reason = "clipped_audio"
    return {
        "duration_ms": duration_ms,
        "active_ms": active_ms,
        "clip_fraction": round(clip_fraction, 6),
        "clipped_samples": clipped,
        "rms_dbfs": round(20 * math.log10(max(rms, 1e-10)), 2),
        "usable_for_clone": reason is None,
        "rejection_reason": reason,
        "sample_rate": SAMPLE_RATE,
        "screening": "energy_and_clipping_v1",
        "speaker_verified": False,
    }, active_windows


def _metrics(pcm: bytes) -> dict:
    return _analyze_pcm(pcm)[0]


def _included_spans(active_windows: list[tuple[int, int]], sample_count: int) -> list[tuple[int, int]]:
    """Retain audible windows plus context; never edit samples inside a span.

    Overlapping context merges, retaining natural pauses up to 400 ms. Longer
    low-energy gaps retain 200 ms at either side. This remains energy screening,
    not a claim that all discarded material is linguistically silent.
    """
    padding = SAMPLE_RATE * ACTIVITY_CONTEXT_MS // 1000
    spans = []
    for start, end in active_windows:
        start, end = max(0, start - padding), min(sample_count, end + padding)
        if spans and start <= spans[-1][1]:
            spans[-1] = (spans[-1][0], max(spans[-1][1], end))
        else:
            spans.append((start, end))
    return spans


def validate_audio_chunk(path: Path, *, mime: str, expected_sha256: str | None = None,
                         max_duration_ms: int = 15000) -> dict:
    data = _read_private_file(Path(path), CHUNK_MAX_BYTES)
    digest = hashlib.sha256(data).hexdigest()
    if expected_sha256 is not None and expected_sha256 != digest:
        raise AudioValidationError("audio_checksum_mismatch", "Saved audio no longer matches its acknowledged checksum.")
    pcm = _decode(data, mime, max_duration_ms)
    return _metrics(pcm) | {"sha256": digest, "byte_count": len(data)}


def build_capture_manifest(chunks: list[dict], *, final_seq: int) -> dict:
    if not isinstance(final_seq, int) or isinstance(final_seq, bool) or not 0 <= final_seq < MAX_SOURCE_CHUNKS:
        raise AudioValidationError("invalid_final_sequence", "The final recording sequence is invalid.")
    ordered = sorted(chunks, key=lambda item: item["seq"])
    if len(ordered) != final_seq + 1 or any(item["seq"] != index for index, item in enumerate(ordered)):
        raise AudioValidationError("audio_sequence_gap", "Some recording segments have not been saved. Finish uploading before preparing the voice.")
    entries = []
    for item in ordered:
        if item.get("role", "contributor") != "contributor":
            raise AudioValidationError("invalid_speaker_role", "Only the contributor microphone can be used for a voice sample.")
        if not re.fullmatch(r"[0-9a-f]{64}", str(item.get("sha256", ""))):
            raise AudioValidationError("invalid_audio_manifest", "The recording manifest has an invalid checksum.")
        if not isinstance(item.get("byte_count"), int) or not 0 < item["byte_count"] <= CHUNK_MAX_BYTES:
            raise AudioValidationError("invalid_audio_manifest", "The recording manifest has an invalid size.")
        mime = str(item.get("mime", "")).split(";", 1)[0]
        if mime not in FORMATS or mime == "audio/mpeg":
            raise AudioValidationError("unsupported_audio", "The recording manifest has an unsupported container.")
        entries.append({"seq": item["seq"], "sha256": item["sha256"], "byte_count": item["byte_count"], "mime": mime})
    manifest = {"schema": "capture_manifest_v1", "last_seq": final_seq, "chunks": entries}
    serial = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    return manifest | {"digest": hashlib.sha256(serial.encode()).hexdigest()}


def prepare_clone_sample(chunks: list[dict], output_dir: Path, *, min_active_ms: int = 60000,
                         max_sample_ms: int = 125000, final_seq: int | None = None,
                         min_sample_ms: int | None = None) -> tuple[list[dict], dict]:
    """Rank a bounded candidate set, then reproduce selected source spans.

    At most 128 unique source chunks are inspected and at most 128 are decoded
    again for assembly, within the same 60 s deadline. Only span metadata is
    retained between passes; PCM buffers contain one source plus <=125 s output.
    Runtime and energy activity are separate local heuristics, not measured
    speech duration, speaker verification, or a provider quality guarantee.
    """
    min_sample_ms = min_active_ms if min_sample_ms is None else min_sample_ms
    if not 1 <= min_active_ms <= min_sample_ms <= max_sample_ms <= 125000:
        raise AudioValidationError("invalid_audio_limits", "Audio preparation limits are invalid.")
    ordered = sorted(chunks, key=lambda item: item["seq"])
    inspected = 0
    decoder_invocations = 0
    decoded_ms = 0
    decoded_active_ms = 0
    rejected = []
    rejected_reasons = {}
    joined = bytearray()
    active_samples = 0
    selected_samples = 0
    saved_reported_ms = sum(max(0, int(item.get("duration_ms", 0))) for item in ordered)

    def diagnostics(reason=None):
        result = {
            "saved_reported_ms": saved_reported_ms, "captured_chunks": len(ordered),
            "inspected_chunks": inspected, "decoder_invocations": decoder_invocations,
            "decoded_source_ms": decoded_ms, "decoded_active_ms": decoded_active_ms,
            "selected_active_ms": round(active_samples * 1000 / SAMPLE_RATE),
            "selected_duration_ms": round(selected_samples * 1000 / SAMPLE_RATE),
            "minimum_active_ms": min_active_ms, "minimum_sample_ms": min_sample_ms,
            "rejected_chunks": len(rejected), "rejected_reasons": dict(rejected_reasons),
            "partial": inspected < len(ordered),
        }
        if reason:
            result["insufficiency"] = reason
        return result

    def require_minimum():
        low_runtime = selected_samples * 1000 < min_sample_ms * SAMPLE_RATE
        low_activity = active_samples * 1000 < min_active_ms * SAMPLE_RATE
        if not low_runtime and not low_activity:
            return
        reason = "sample_and_activity" if low_runtime and low_activity else "sample_duration" if low_runtime else "audible_activity"
        partial = inspected < len(ordered)
        message = (
            "The bounded audio analysis could not select a sufficient sample; the rest of your saved recording has not been ruled out."
            if partial else "The analyzed recording did not provide enough usable sample runtime and acoustic activity for this local quality check."
        )
        raise AudioValidationError("audio_analysis_incomplete" if partial else "insufficient_audio", message,
                                   details=diagnostics(reason))

    if not ordered:
        require_minimum()
    capture = build_capture_manifest(ordered, final_seq=ordered[-1]["seq"] if final_seq is None else final_seq)
    count = min(len(ordered), MAX_DECODE_CHUNKS)
    indexes = sorted({round(index * (len(ordered) - 1) / max(1, count - 1)) for index in range(count)})
    deadline = time.monotonic() + PREPARE_TIMEOUT_SECONDS

    def remaining_time():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AudioValidationError("audio_prepare_timeout", "Audio preparation took too long; your saved recording is still available.", details=diagnostics())
        return remaining

    def reject(seq, reason):
        rejected.append({"seq": seq, "reason": reason})
        rejected_reasons[reason] = rejected_reasons.get(reason, 0) + 1

    def read_source(item):
        data = _read_private_file(Path(item["path"]), CHUNK_MAX_BYTES)
        if len(data) != item["byte_count"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise AudioValidationError("audio_checksum_mismatch", "Saved audio no longer matches its acknowledged checksum.")
        return data

    # Pass one holds source PCM only while measuring it. Do not fill the output
    # in chronological first-fit order: later candidates may contain clearer
    # material than the first 125 seconds encountered.
    candidates = []
    sources = {}
    for index in indexes:
        remaining_time()
        item = ordered[index]
        inspected += 1
        try:
            data = read_source(item)
            budget = remaining_time()
            decoder_invocations += 1
            pcm = _decode(data, item["mime"], 15000, timeout_seconds=budget)
            source_metrics, active_windows = _analyze_pcm(pcm)
        except DecoderUnavailable:
            raise
        except AudioValidationError as exc:
            if exc.code == "audio_prepare_timeout":
                raise
            reject(item["seq"], exc.code)
            continue
        remaining_time()
        decoded_ms += source_metrics["duration_ms"]
        decoded_active_ms += source_metrics["active_ms"]
        if source_metrics["clipped_samples"] > (len(pcm) // 2) * 0.005:
            reject(item["seq"], "clipped_audio")
            continue
        if source_metrics["active_ms"] < 150:
            reject(item["seq"], "insufficient_audible_audio")
            continue
        spans = _included_spans(active_windows, len(pcm) // 2)
        retained_metrics, _ = _analyze_pcm(pcm, spans)
        if not retained_metrics["usable_for_clone"]:
            reject(item["seq"], retained_metrics["rejection_reason"])
            continue
        available = []
        for start, end in spans:
            metrics, windows = _analyze_pcm(pcm, [(start, end)])
            if not metrics["usable_for_clone"]:
                continue
            available.append({"seq": item["seq"], "start": start, "end": end,
                              "active_samples": sum(b - a for a, b in windows)})
        if not available:
            reject(item["seq"], "insufficient_audible_audio")
            continue
        sources[item["seq"]] = {"item": item, "metrics": source_metrics}
        candidates.extend(available)
    remaining_time()
    # Free the last first-pass payload before beginning assembly.
    if indexes:
        data = b""
        pcm = b""
    ranked = sorted(candidates, key=lambda span: (-span["active_samples"] / (span["end"] - span["start"]), span["seq"], span["start"]))
    max_samples = SAMPLE_RATE * max_sample_ms // 1000
    chosen = {}
    for span in ranked:
        length = span["end"] - span["start"]
        if selected_samples + length <= max_samples:
            chosen.setdefault(span["seq"], []).append(span)
            selected_samples += length
            active_samples += span["active_samples"]
    require_minimum()

    # Pass two verifies the original bytes again, then copies selected complete
    # contextual spans in source order. Never reuse metadata for changed audio.
    selected = []
    assembled_activity = 0
    for seq in sorted(chosen):
        remaining_time()
        source = sources[seq]
        item = source["item"]
        data = read_source(item)
        budget = remaining_time()
        decoder_invocations += 1
        pcm = _decode(data, item["mime"], 15000, timeout_seconds=budget)
        source_metrics, _ = _analyze_pcm(pcm)
        spans = sorted(chosen[seq], key=lambda span: span["start"])
        coordinates = [(span["start"], span["end"]) for span in spans]
        if source_metrics != source["metrics"] or any(start < 0 or end > len(pcm) // 2 for start, end in coordinates):
            raise AudioValidationError("audio_analysis_changed", "Saved audio no longer matches its measured sample; preparation stopped.", details=diagnostics())
        retained_metrics, _ = _analyze_pcm(pcm, coordinates)
        if not retained_metrics["usable_for_clone"]:
            raise AudioValidationError("audio_analysis_changed", "The selected audio no longer passes its measured quality check.", details=diagnostics())
        remaining_time()
        included = []
        chunk_samples = 0
        chunk_activity = 0
        for span in spans:
            start, end = span["start"], span["end"]
            output_start = len(joined) // 2
            joined.extend(memoryview(pcm)[start * 2:end * 2])
            included.append({"start_sample": start, "end_sample": end,
                             "output_start_sample": output_start, "output_end_sample": len(joined) // 2,
                             "active_ms": round(span["active_samples"] * 1000 / SAMPLE_RATE)})
            chunk_samples += end - start
            chunk_activity += span["active_samples"]
        assembled_activity += chunk_activity
        selected.append({"seq": seq, "sha256": item["sha256"], "byte_count": len(data),
                         "mime": item["mime"], "duration_ms": round(chunk_samples * 1000 / SAMPLE_RATE),
                         "active_ms": round(chunk_activity * 1000 / SAMPLE_RATE),
                         "clip_fraction": retained_metrics["clip_fraction"],
                         "decoded_source": source_metrics, "included_spans": included})
    remaining_time()
    if len(joined) // 2 != selected_samples or assembled_activity != active_samples:
        raise AudioValidationError("audio_analysis_changed", "Audio sample assembly did not match its measured manifest.", details=diagnostics())
    active_ms = round(active_samples * 1000 / SAMPLE_RATE)
    duration_ms = round(selected_samples * 1000 / SAMPLE_RATE)
    manifest = {
        "provider": "elevenlabs", "selection": "decoded_ranked_context_spans_v3",
        "capture_digest": capture["digest"], "last_seq": capture["last_seq"],
        "chunks": selected, "rejected": rejected, "total_ms": duration_ms,
        "active_ms": active_ms, "sample_rate": SAMPLE_RATE,
        "context_ms": ACTIVITY_CONTEXT_MS, "diagnostics": diagnostics(),
        "speaker_verified": False, "human_listening_required": True,
        "screening_limits": "Local runtime, energy and clipping heuristics only; activity is not speech duration or a provider requirement. Does not establish speaker identity, remove other voices, or prove noise-free speech. Retained contextual PCM samples are unchanged.",
    }
    output_dir = Path(output_dir)
    if output_dir.is_symlink():
        raise AudioValidationError("invalid_audio_directory", "Audio sample storage is unavailable.")
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp_name = tempfile.mkstemp(prefix="sample-", suffix=".wav", dir=output_dir)
    path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            with wave.open(handle, "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(SAMPLE_RATE)
                audio.writeframes(joined)
        with path.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        remaining_time()
        byte_count = path.stat().st_size
        manifest["normalized_sha256"] = digest
        manifest["normalized_byte_count"] = byte_count
        return [{"path": str(path), "mime": "audio/wav", "seq": 0, "sha256": digest,
                 "byte_count": byte_count, "duration_ms": duration_ms}], manifest
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def validate_synthesized_audio(data: bytes, *, mime: str = "audio/mpeg", max_duration_ms: int = 30000) -> dict:
    if not isinstance(data, bytes) or not data or len(data) > 5 * 1024 * 1024:
        raise AudioValidationError("invalid_preview_audio", "The voice provider did not return a bounded audio preview.")
    if not 100 <= max_duration_ms <= 120000:
        raise AudioValidationError("invalid_audio_limits", "Preview duration limit is invalid.")
    pcm = _decode(data, mime, max_duration_ms)
    metrics = _metrics(pcm)
    if metrics["active_ms"] < 100:
        raise AudioValidationError("silent_preview_audio", "The generated preview has no audible content.")
    return metrics | {"sha256": hashlib.sha256(data).hexdigest(), "byte_count": len(data)}
