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
CHUNK_MAX_BYTES = 80 * 1024
MAX_SOURCE_CHUNKS = 10001
MAX_DECODE_CHUNKS = 128
DECODE_TIMEOUT_SECONDS = 8
PREPARE_TIMEOUT_SECONDS = 60
FORMATS = {"audio/webm": "matroska,webm", "audio/ogg": "ogg", "audio/wav": "wav", "audio/x-wav": "wav", "audio/mpeg": "mp3"}


class AudioValidationError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
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


def _decode(data: bytes, mime: str, max_duration_ms: int) -> bytes:
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
                                stderr=subprocess.DEVNULL, timeout=DECODE_TIMEOUT_SECONDS,
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


def _metrics(pcm: bytes) -> dict:
    samples = array("h")
    samples.frombytes(pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    window_size = SAMPLE_RATE // 50  # 20 ms windows, fixed -40 dBFS threshold.
    active_samples = 0
    clipped = 0
    square_sum = 0
    for start in range(0, len(samples), window_size):
        window = samples[start:start + window_size]
        energy = sum(value * value for value in window)
        square_sum += energy
        clipped += sum(abs(value) >= 32700 for value in window)
        if energy >= len(window) * (32768 * 0.01) ** 2:
            active_samples += len(window)
    duration_ms = round(len(samples) * 1000 / SAMPLE_RATE)
    active_ms = round(active_samples * 1000 / SAMPLE_RATE)
    clip_fraction = clipped / len(samples)
    rms = math.sqrt(square_sum / len(samples)) / 32768
    reason = None
    if active_ms < max(150, duration_ms * 0.35):
        reason = "insufficient_audible_audio"
    elif clip_fraction > 0.005:
        reason = "clipped_audio"
    return {
        "duration_ms": duration_ms,
        "active_ms": active_ms,
        "clip_fraction": round(clip_fraction, 6),
        "rms_dbfs": round(20 * math.log10(max(rms, 1e-10)), 2),
        "usable_for_clone": reason is None,
        "rejection_reason": reason,
        "sample_rate": SAMPLE_RATE,
        "screening": "energy_and_clipping_v1",
        "speaker_verified": False,
    }


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
                         max_sample_ms: int = 125000, final_seq: int | None = None) -> tuple[list[dict], dict]:
    """Return one private normalized WAV and a deterministic source manifest.

    The full capture is acknowledged before selection. Up to 128 source chunks
    are decoded in spread order; RAM holds at most one source PCM plus 125 s of
    mono 24 kHz output (~6 MB). Rejected chunks never enter the provider file.
    """
    if not chunks:
        raise AudioValidationError("insufficient_audio", "Record more contributor speech before preparing the voice.")
    if not 1 <= min_active_ms <= max_sample_ms <= 125000:
        raise AudioValidationError("invalid_audio_limits", "Audio preparation limits are invalid.")
    ordered = sorted(chunks, key=lambda item: item["seq"])
    capture = build_capture_manifest(ordered, final_seq=ordered[-1]["seq"] if final_seq is None else final_seq)
    # Equally spread candidates bound decoding work even for a long interview.
    count = min(len(ordered), MAX_DECODE_CHUNKS)
    indexes = sorted({round(index * (len(ordered) - 1) / max(1, count - 1)) for index in range(count)})
    selected = []
    rejected = []
    joined = bytearray()
    active_ms = 0
    duration_ms = 0
    deadline = time.monotonic() + PREPARE_TIMEOUT_SECONDS
    for index in indexes:
        if duration_ms >= max_sample_ms:
            break
        if time.monotonic() > deadline:
            raise AudioValidationError("audio_prepare_timeout", "Audio preparation took too long; your saved recording is still available.")
        item = ordered[index]
        try:
            data = _read_private_file(Path(item["path"]), CHUNK_MAX_BYTES)
            if len(data) != item["byte_count"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
                raise AudioValidationError("audio_checksum_mismatch", "Saved audio no longer matches its acknowledged checksum.")
            pcm = _decode(data, item["mime"], 15000)
            metrics = _metrics(pcm)
        except DecoderUnavailable:
            raise
        except AudioValidationError as exc:
            rejected.append({"seq": item["seq"], "reason": exc.code})
            continue
        if not metrics["usable_for_clone"]:
            rejected.append({"seq": item["seq"], "reason": metrics["rejection_reason"]})
            continue
        if duration_ms + metrics["duration_ms"] > max_sample_ms:
            continue
        joined.extend(pcm)
        active_ms += metrics["active_ms"]
        duration_ms += metrics["duration_ms"]
        selected.append({"seq": item["seq"], "sha256": item["sha256"], "byte_count": len(data),
                         "mime": item["mime"], "duration_ms": metrics["duration_ms"],
                         "active_ms": metrics["active_ms"], "clip_fraction": metrics["clip_fraction"]})
    if active_ms < min_active_ms:
        raise AudioValidationError("insufficient_audio", f"Need more clear contributor audio before preparing the voice ({active_ms // 1000}s audible audio selected; {min_active_ms // 1000}s required).")
    manifest = {
        "provider": "elevenlabs", "selection": "decoded_spread_energy_v1",
        "capture_digest": capture["digest"], "last_seq": capture["last_seq"],
        "chunks": selected, "rejected": rejected, "total_ms": duration_ms,
        "active_ms": active_ms, "sample_rate": SAMPLE_RATE,
        "speaker_verified": False, "human_listening_required": True,
        "screening_limits": "Energy and clipping only; does not establish speaker identity, remove other voices, or prove noise-free speech.",
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
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        manifest["normalized_sha256"] = digest
        manifest["normalized_byte_count"] = len(data)
        return [{"path": str(path), "mime": "audio/wav", "seq": 0, "sha256": digest,
                 "byte_count": len(data), "duration_ms": duration_ms}], manifest
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
