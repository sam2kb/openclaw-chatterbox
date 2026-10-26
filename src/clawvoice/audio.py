"""Bounded FFmpeg/FFprobe audio handling and native-rate WAV assembly."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Settings
from .errors import UnavailableError, ValidationFailure
from .modelspec import TURBO_DECODER_CONDITIONING_MS, TURBO_ENCODER_CONDITIONING_MS
from .util import ensure_private_dir, sha256_file

_ALLOWED_FORMATS = {
    "wav",
    "wave",
    "mp3",
    "ogg",
    "webm",
    "matroska",
    "mov",
    "mp4",
    "m4a",
    "3gp",
    "3g2",
    "mj2",
    "flac",
    "aac",
    "aiff",
    "aif",
    "caf",
}


class AudioDecodeError(ValidationFailure):
    code = "audio_decode_failed"


@dataclass
class ProbeResult:
    duration_ms: int
    sample_rate: int
    channels: int
    codec: str
    format_name: str
    size_bytes: int
    duration_seconds: float
    duration_source: str = "ffprobe"


@dataclass
class Checks:
    warnings: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    suitable: bool = False
    suggested_start_ms: int = 0
    suggested_end_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "suitable": self.suitable,
            "warnings": self.warnings,
            "metrics": self.metrics,
            "suggested_segment": {
                "start_ms": self.suggested_start_ms,
                "end_ms": self.suggested_end_ms,
            },
            "speaker_identity_verified": False,
            "background_noise_removed": False,
        }


class AudioProcessor:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        for binary in (settings.ffmpeg, settings.ffprobe):
            if shutil.which(binary) is None:
                raise UnavailableError(
                    f"{binary} is not installed or not on PATH",
                    details={"binary": binary},
                )

    def _run(self, args: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                args,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env={"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")},
            )
        except subprocess.TimeoutExpired as exc:
            raise AudioDecodeError(
                "audio decoding timed out; try a shorter or simpler file",
                details={"stage": "decode_timeout"},
            ) from exc
        except OSError as exc:
            raise UnavailableError("audio decoder could not be started") from exc

    def probe(self, path: Path, *, enforce_reference_limit: bool = True) -> ProbeResult:
        args = [
            self.settings.ffprobe,
            "-v",
            "error",
            "-protocol_whitelist",
            "file",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_name,codec_type,sample_rate,channels:format=duration,format_name,size",
            "-of",
            "json",
            str(path),
        ]
        proc = self._run(args, timeout=self.settings.probe_timeout_seconds)
        if proc.returncode != 0:
            raise AudioDecodeError(
                "the upload is not a usable audio file",
                details={"stage": "probe"},
            )
        try:
            payload = json.loads(proc.stdout or "{}")
            stream = payload["streams"][0]
            fmt = payload.get("format", {})
            duration = float(fmt.get("duration") or stream.get("duration") or 0)
            sample_rate = int(stream["sample_rate"])
            channels = int(stream["channels"])
            codec = str(stream.get("codec_name") or "unknown")
            format_name = str(fmt.get("format_name") or "unknown")
            size = int(fmt.get("size") or path.stat().st_size)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise AudioDecodeError("the upload has no decodable audio stream") from exc
        format_tokens = {token.strip() for token in format_name.split(",")}
        if not (format_tokens & _ALLOWED_FORMATS):
            raise AudioDecodeError(
                "the upload container is not on the supported audio format list",
                details={"format": format_name},
            )
        if sample_rate <= 0 or channels <= 0:
            raise AudioDecodeError("the upload reports an invalid audio layout")
        duration_source = "ffprobe"
        if not math.isfinite(duration) or duration <= 0:
            # Many MediaRecorder WebM/Opus blobs have no duration in their
            # header. Decode a bounded mono window to determine it rather than
            # rejecting a perfectly usable browser recording.
            duration_ms = self._decoded_duration_probe(path)
            duration = duration_ms / 1000.0
            duration_source = "decoded"
        else:
            duration_ms = int(round(duration * 1000))
        if enforce_reference_limit and duration_ms > self.settings.max_reference_decoded_ms:
            raise ValidationFailure(
                "the reference recording is longer than the configured limit",
                details={
                    "duration_ms": duration_ms,
                    "max_decoded_ms": self.settings.max_reference_decoded_ms,
                },
            )
        if duration_ms <= 0:
            raise AudioDecodeError("the upload has no measurable audio duration")
        return ProbeResult(
            duration_ms=duration_ms,
            sample_rate=sample_rate,
            channels=channels,
            codec=codec,
            format_name=format_name,
            size_bytes=size,
            duration_seconds=duration,
            duration_source=duration_source,
        )

    def _decoded_duration_probe(self, path: Path) -> int:
        """Bounded fallback duration probe for headerless/live-style containers."""
        ensure_private_dir(self.settings.tmp_dir)
        fd, tmp_name = tempfile.mkstemp(prefix="duration-", suffix=".wav", dir=self.settings.tmp_dir)
        os.close(fd)
        tmp = Path(tmp_name)
        max_ms = self.settings.max_reference_decoded_ms
        max_bytes = int(24_000 * (max_ms / 1000.0 + 2.0) * 2) + 1024 * 1024
        args = [
            self.settings.ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-protocol_whitelist",
            "file",
            "-y",
            "-i",
            str(path),
            "-t",
            f"{max_ms / 1000.0 + 1.0:.3f}",
            "-map",
            "0:a:0",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "24000",
            "-c:a",
            "pcm_s16le",
            "-fs",
            str(max_bytes),
            "-f",
            "wav",
            str(tmp),
        ]
        try:
            proc = self._run(args, timeout=self.settings.decoder_timeout_seconds)
            if proc.returncode != 0 or not tmp.is_file() or tmp.stat().st_size <= 44:
                raise AudioDecodeError("the upload has no decodable audio duration")
            info = self.wav_info(tmp)
            duration_ms = int(round(info["frames"] / info["sample_rate"] * 1000))
            if duration_ms >= max_ms + 500:
                raise ValidationFailure(
                    "the reference recording is longer than the configured limit",
                    details={"duration_ms": duration_ms, "max_decoded_ms": max_ms},
                )
            return duration_ms
        finally:
            tmp.unlink(missing_ok=True)

    def measure(self, path: Path, duration_ms: int) -> dict[str, Any]:
        args = [
            self.settings.ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "info",
            "-protocol_whitelist",
            "file",
            "-i",
            str(path),
            "-vn",
            "-af",
            "silencedetect=noise=-45dB:d=0.5,volumedetect",
            "-f",
            "null",
            "-",
        ]
        proc = self._run(args, timeout=self.settings.decoder_timeout_seconds)
        if proc.returncode != 0:
            raise AudioDecodeError("the audio could not be analyzed", details={"stage": "measure"})
        stderr = proc.stderr or ""
        max_volume = _float_match(stderr, r"max_volume:\s*(-?\d+(?:\.\d+)?) dB")
        mean_volume = _float_match(stderr, r"mean_volume:\s*(-?\d+(?:\.\d+)?) dB")
        silent_seconds = 0.0
        last_start: float | None = None
        for line in stderr.splitlines():
            match = re.search(r"silence_start:\s*(-?\d+(?:\.\d+)?)", line)
            if match:
                last_start = max(0.0, float(match.group(1)))
            match = re.search(r"silence_end:\s*(-?\d+(?:\.\d+)?).*?silence_duration:\s*(-?\d+(?:\.\d+)?)", line)
            if match:
                silent_seconds += max(0.0, float(match.group(2)))
                last_start = None
        if last_start is not None:
            silent_seconds += max(0.0, duration_ms / 1000.0 - last_start)
        duration_s = max(0.001, duration_ms / 1000.0)
        silence_ratio = min(1.0, silent_seconds / duration_s)
        return {
            "max_volume_db": max_volume,
            "mean_volume_db": mean_volume,
            "silence_ratio": round(silence_ratio, 4),
            "silence_seconds": round(silent_seconds, 3),
            "possible_clipping": max_volume is not None and max_volume >= -0.1,
            "analysis": "ffmpeg volumedetect/silencedetect",
            "speaker_identity_verified": False,
            "background_noise_analysis": "not_performed",
            "denoised": False,
        }

    def inspect(self, path: Path, probe: ProbeResult | None = None) -> tuple[ProbeResult, Checks]:
        probe = probe or self.probe(path)
        metrics = self.measure(path, probe.duration_ms)
        start, end = self.suggest_segment(probe.duration_ms)
        warnings: list[str] = []
        if probe.duration_ms < 6_000:
            warnings.append("too_short")
        if probe.duration_ms > 90_000:
            warnings.append("long_source_select_a_clip")
        if not 10_000 <= (end - start) <= 15_000:
            warnings.append("outside_recommended_10_15_seconds")
        if metrics["possible_clipping"]:
            warnings.append("possible_clipping")
        if metrics["max_volume_db"] is not None and metrics["max_volume_db"] < -35:
            warnings.append("very_quiet")
        if metrics["silence_ratio"] > 0.35:
            warnings.append("mostly_silent")
        if probe.channels > 1:
            warnings.append("source_is_stereo_will_be_downmixed")
        if probe.sample_rate < 16_000:
            warnings.append("low_source_sample_rate")
        warnings.append("speaker_identity_not_verified")
        warnings.append("background_noise_not_removed")
        suitable = (
            (end - start) >= 5_001
            and not metrics["possible_clipping"]
            and metrics["silence_ratio"] <= 0.6
        )
        checks = Checks(
            warnings=warnings,
            metrics={
                **metrics,
                "source_duration_ms": probe.duration_ms,
                "source_sample_rate": probe.sample_rate,
                "source_channels": probe.channels,
                "codec": probe.codec,
                "format": probe.format_name,
                "duration_source": probe.duration_source,
            },
            suitable=suitable,
            suggested_start_ms=start,
            suggested_end_ms=end,
        )
        return probe, checks

    def suggest_segment(self, duration_ms: int) -> tuple[int, int]:
        """Explicit, non-silent assumption: first 15 s, shown to the caller/UI."""
        end = min(duration_ms, 15_000)
        start = 0
        if end - start < 5_001 and duration_ms > 5_001:
            start = max(0, duration_ms - 15_000)
            end = duration_ms
        return start, end

    def validate_segment(
        self, duration_ms: int, start_ms: int | None, end_ms: int | None
    ) -> tuple[int, int]:
        suggested = self.suggest_segment(duration_ms)
        start = suggested[0] if start_ms is None else int(start_ms)
        end = suggested[1] if end_ms is None else int(end_ms)
        if start < 0 or end <= start:
            raise ValidationFailure("the reference segment must have a positive duration")
        if end > duration_ms:
            raise ValidationFailure(
                "the reference segment extends beyond the recording",
                details={"source_duration_ms": duration_ms, "end_ms": end},
            )
        length = end - start
        if length < 5_001:
            raise ValidationFailure(
                "Chatterbox requires more than five seconds of reference audio; select a longer clip",
                details={"segment_duration_ms": length, "minimum_ms": 5_001},
            )
        if length > self.settings.reference_max_ms:
            raise ValidationFailure(
                "the selected reference clip is longer than the pinned backend accepts",
                details={"segment_duration_ms": length, "maximum_ms": self.settings.reference_max_ms},
            )
        return start, end

    def normalize(
        self,
        source: Path,
        output: Path,
        *,
        start_ms: int,
        end_ms: int,
        sample_rate: int | None = None,
    ) -> int:
        sample_rate = sample_rate or self.settings.reference_sample_rate
        duration_ms = end_ms - start_ms
        if duration_ms <= 0:
            raise ValidationFailure("the reference segment has no duration")
        max_bytes = int(sample_rate * (duration_ms / 1000.0) * 2) + 1024 * 1024
        ensure_private_dir(output.parent)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
        os.close(fd)
        tmp = Path(tmp_name)
        args = [
            self.settings.ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-protocol_whitelist",
            "file",
            "-y",
            "-i",
            str(source),
            "-ss",
            f"{start_ms / 1000:.3f}",
            "-t",
            f"{duration_ms / 1000:.3f}",
            "-map",
            "0:a:0",
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-c:a",
            "pcm_s16le",
            "-fs",
            str(max_bytes),
            "-f",
            "wav",
            str(tmp),
        ]
        try:
            proc = self._run(args, timeout=self.settings.decoder_timeout_seconds)
            if proc.returncode != 0:
                raise AudioDecodeError(
                    "the selected clip could not be decoded",
                    details={"stage": "normalize"},
                )
            info = self.wav_info(tmp)
            if info["channels"] != 1 or info["sample_rate"] != sample_rate:
                raise AudioDecodeError("the decoder produced an unexpected audio format")
            produced_ms = int(round(info["frames"] / sample_rate * 1000))
            if abs(produced_ms - duration_ms) > 150:
                raise AudioDecodeError(
                    "the decoded clip duration is shorter than requested; the source may be truncated",
                    details={"requested_ms": duration_ms, "decoded_ms": produced_ms},
                )
            os.chmod(tmp, 0o600)
            os.replace(tmp, output)
        except BaseException:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
            raise
        return int(round(self.wav_info(output)["frames"] / sample_rate * 1000))

    def checks_for_selection(
        self,
        probe: ProbeResult,
        checks: Checks,
        start_ms: int,
        end_ms: int,
    ) -> dict[str, Any]:
        selected = dict(checks.as_dict())
        selected["suggested_segment"] = {"start_ms": start_ms, "end_ms": end_ms}
        length = end_ms - start_ms
        selected["selected_duration_ms"] = length
        selected["conditioning_windows"] = {
            "encoder_ms": TURBO_ENCODER_CONDITIONING_MS,
            "decoder_ms": TURBO_DECODER_CONDITIONING_MS,
            "requested_ms": length,
        }
        return selected

    def write_wav(self, path: Path, pcm: bytes, sample_rate: int) -> int:
        if len(pcm) % 2:
            raise ValidationFailure("internal audio buffer has an invalid length")
        ensure_private_dir(path.parent)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as raw:
                with wave.open(raw, "wb") as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)
                    wav.setframerate(sample_rate)
                    wav.writeframes(pcm)
                raw.flush()
                os.fsync(raw.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
            raise
        frames = len(pcm) // 2
        return int(round(frames / sample_rate * 1000))

    def wav_info(self, path: Path) -> dict[str, Any]:
        try:
            with wave.open(str(path), "rb") as wav:
                return {
                    "channels": wav.getnchannels(),
                    "sample_rate": wav.getframerate(),
                    "sample_width": wav.getsampwidth(),
                    "frames": wav.getnframes(),
                    "duration_ms": int(round(wav.getnframes() / wav.getframerate() * 1000)),
                }
        except (wave.Error, EOFError, OSError) as exc:
            raise AudioDecodeError("the generated WAV is invalid") from exc

    def convert(self, master: Path, output: Path, preset: str) -> dict[str, Any]:
        if preset == "master":
            raise ValueError("master does not need conversion")
        ensure_private_dir(output.parent)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
        os.close(fd)
        tmp = Path(tmp_name)
        if preset == "mp3":
            args = [
                self.settings.ffmpeg,
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-protocol_whitelist",
                "file",
                "-y",
                "-i",
                str(master),
                "-ac",
                "1",
                "-codec:a",
                "libmp3lame",
                "-b:a",
                "128k",
                "-f",
                "mp3",
                str(tmp),
            ]
        elif preset == "telephony":
            args = [
                self.settings.ffmpeg,
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-protocol_whitelist",
                "file",
                "-y",
                "-i",
                str(master),
                "-ac",
                "1",
                "-ar",
                "8000",
                "-c:a",
                "pcm_s16le",
                "-f",
                "wav",
                str(tmp),
            ]
        elif preset == "telephony-mulaw":
            args = [
                self.settings.ffmpeg,
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-protocol_whitelist",
                "file",
                "-y",
                "-i",
                str(master),
                "-ac",
                "1",
                "-ar",
                "8000",
                "-c:a",
                "pcm_mulaw",
                "-f",
                "mulaw",
                str(tmp),
            ]
        else:
            raise ValidationFailure(f"unsupported output preset: {preset}")
        try:
            proc = self._run(args, timeout=self.settings.decoder_timeout_seconds)
            if proc.returncode != 0:
                raise UnavailableError(
                    f"FFmpeg could not produce the {preset} output",
                    details={"preset": preset},
                )
            if not tmp.is_file() or tmp.stat().st_size == 0:
                raise UnavailableError(f"FFmpeg produced an empty {preset} output")
            os.chmod(tmp, 0o600)
            os.replace(tmp, output)
        except BaseException:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
            raise
        size = output.stat().st_size
        if preset == "mp3":
            probe = self.probe(output, enforce_reference_limit=False)
            return {
                "media_type": "audio/mpeg",
                "format": "mp3",
                "duration_ms": probe.duration_ms,
                "sample_rate": probe.sample_rate,
                "channels": probe.channels,
                "size_bytes": size,
                "sha256": sha256_file(output),
            }
        if preset == "telephony":
            info = self.wav_info(output)
            if info["sample_rate"] != 8000 or info["channels"] != 1:
                raise UnavailableError("telephony output did not have the expected 8 kHz mono layout")
            return {
                "media_type": "audio/wav",
                "format": "wav/pcm_s16le_8k_mono",
                "duration_ms": info["duration_ms"],
                "sample_rate": info["sample_rate"],
                "channels": info["channels"],
                "size_bytes": size,
                "sha256": sha256_file(output),
            }
        if preset == "telephony-mulaw":
            if size <= 0:
                raise UnavailableError("mu-law output is empty")
            return {
                "media_type": "audio/x-mulaw",
                "format": "raw/mulaw_8k_mono",
                "duration_ms": int(round(size * 1000 / 8000)),
                "sample_rate": 8000,
                "channels": 1,
                "size_bytes": size,
                "sha256": sha256_file(output),
            }
        raise AssertionError("unreachable")

    def silence_pcm(self, ms: int, sample_rate: int) -> bytes:
        if ms < 0:
            raise ValidationFailure("pause duration cannot be negative")
        samples = int(round(sample_rate * ms / 1000.0))
        return b"\x00\x00" * samples


def _float_match(text: str, pattern: str) -> float | None:
    match = re.search(pattern, text)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None
