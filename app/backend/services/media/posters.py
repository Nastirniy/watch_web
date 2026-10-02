"""Video poster extraction with ffmpeg. Pure functions, no database, no asyncio.

One JPEG per video, capped at poster.max_width, written atomically
(temp file + os.replace). imgproxy renders every thumbnail size from it.

ffmpeg runs as a subprocess so it can be killed on timeout and takes its
memory with it when it exits.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from app.backend.config import PosterSettings

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
NEAR_BLACK_LUMINANCE = 16


class PosterError(Exception):
    """ffmpeg failed, timed out, or produced nothing usable"""


@dataclass
class PosterResult:
    seek_seconds: float
    duration_seconds: Optional[float]
    retried_black: bool = False


def resolve_binary(configured: str, imageio_fallback: bool = False) -> Optional[str]:
    """Absolute path of an ffmpeg tool: explicit path, then PATH lookup, then the imageio-ffmpeg static binary"""
    if not configured:
        configured = "ffmpeg"
    candidate = Path(configured)
    if candidate.is_absolute() or candidate.parent != Path("."):
        return str(candidate) if candidate.exists() else None
    found = shutil.which(configured)
    if found:
        return found
    if imageio_fallback:
        try:
            import imageio_ffmpeg  # optional dependency, ships a static ffmpeg
            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            return None
    return None


def _run(command: List[str], timeout: float) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise PosterError(f"{Path(command[0]).name} timed out after {timeout:.0f}s") from exc
    except OSError as exc:
        raise PosterError(f"cannot run {command[0]}: {exc}") from exc


def probe_duration(source: Path, ffmpeg: str, ffprobe: Optional[str], timeout: float = 20) -> Optional[float]:
    """Duration in seconds via ffprobe, falling back to ffmpeg's banner when ffprobe is absent"""
    if ffprobe:
        proc = _run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(source)],
            timeout,
        )
        text = proc.stdout.decode("utf-8", "replace").strip()
        try:
            value = float(text)
            return value if value > 0 else None
        except ValueError:
            pass
    proc = _run([ffmpeg, "-hide_banner", "-nostdin", "-i", str(source)], timeout)
    match = _DURATION_RE.search(proc.stderr.decode("utf-8", "replace"))
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    value = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    return value if value > 0 else None


def extract_frame(source: Path, target: Path, seek_seconds: float, max_width: int, ffmpeg: str, timeout: float) -> None:
    """Write one frame at seek_seconds to target (JPEG). Raises PosterError when no frame comes out."""
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-ss", f"{max(0.0, seek_seconds):.3f}",
        "-i", str(source),
        "-frames:v", "1",
        "-vf", f"scale='min({int(max_width)},iw)':-2",
        "-q:v", "3",
        "-f", "image2",
        str(target),
    ]
    proc = _run(command, timeout)
    if proc.returncode != 0 or not target.exists() or target.stat().st_size == 0:
        stderr = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        detail = stderr[-1] if stderr else f"exit code {proc.returncode}"
        if target.exists():
            target.unlink(missing_ok=True)
        raise PosterError(f"no frame at {seek_seconds:.1f}s: {detail}")


def is_near_black(image_path: Path, threshold: int = NEAR_BLACK_LUMINANCE) -> bool:
    """True when the mean luminance of the frame is below threshold (fade-in, lens cap, black leader)"""
    try:
        from PIL import Image, ImageStat
        with Image.open(image_path) as img:
            grey = img.convert("L")
            grey.thumbnail((64, 64))
            return ImageStat.Stat(grey).mean[0] < threshold
    except Exception:
        return False


def make_poster(
    source: Path,
    target: Path,
    config: PosterSettings,
    ffmpeg: str,
    ffprobe: Optional[str] = None,
) -> PosterResult:
    """Extract the poster frame for a video into target, atomically.

    Seeks to frame_position of the duration (capped at max_seek_seconds),
    falls back to the first frame when that yields nothing, and retries two
    seconds later once when the frame is near black.
    """
    if not source.exists():
        raise PosterError(f"source file missing: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.tmp-{uuid.uuid4().hex[:8]}")

    duration = probe_duration(source, ffmpeg, ffprobe, timeout=min(config.timeout_seconds, 30))
    seek = 0.0
    if duration:
        seek = min(duration * config.frame_position, float(config.max_seek_seconds))
        if seek >= duration:
            seek = 0.0

    retried_black = False
    try:
        try:
            extract_frame(source, tmp, seek, config.max_width, ffmpeg, config.timeout_seconds)
        except PosterError:
            if seek == 0.0:
                raise
            seek = 0.0
            extract_frame(source, tmp, seek, config.max_width, ffmpeg, config.timeout_seconds)

        if is_near_black(tmp) and duration and seek + 2.0 < duration:
            retried_black = True
            retry = tmp.with_name(f"{tmp.name}-retry")
            try:
                extract_frame(source, retry, seek + 2.0, config.max_width, ffmpeg, config.timeout_seconds)
                if not is_near_black(retry):
                    os.replace(retry, tmp)
                    seek += 2.0
                else:
                    retry.unlink(missing_ok=True)
            except PosterError:
                retry.unlink(missing_ok=True)

        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)

    return PosterResult(seek_seconds=seek, duration_seconds=duration, retried_black=retried_black)
