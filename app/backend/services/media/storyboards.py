"""Storyboard layout, client status and WebVTT (pure functions).

A storyboard is a set of sprite sheets (a grid of small frames) plus a WebVTT
file whose cues point at one tile of a sheet with a media fragment.
"""
from __future__ import annotations

import math
import os
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from app.backend.config import StoryboardSettings
from app.backend.enums.general import PosterStatus, StoryboardStatus
from app.backend.models.media import MediaFile
from app.backend.models.storyboard import VideoStoryboard
from app.backend.utils.logging_config import get_logger

_logger = get_logger("storyboards")

NICE_STEPS = (1, 2, 5, 10, 15, 30, 60, 120, 300, 600)
# How ffmpeg says that it read the input but no frame reached the output (6.1 and 7.x; older
# versions exit 0 without a sheet). Seen on a keyframes-only pass over a video with a single keyframe
NOTHING_WRITTEN_MARKERS = (
    "Nothing was written into output file",
    "Could not open encoder before EOF",
    "Output file is empty, nothing was encoded",
)
# How often a running ffmpeg is checked for the timeout and a cancel
POLL_SECONDS = 0.2
# Time ffmpeg gets to exit after terminate() before it is killed
STOP_GRACE_SECONDS = 5


@dataclass(frozen=True)
class StoryboardPlan:
    step_seconds: int
    frame_count: int
    columns: int
    rows: int
    tile_width: int
    sheet_count: int
    # Container duration the plan was made for; ffmpeg repeats the last frame up to it
    duration_seconds: float


def _nice_step(raw: float) -> int:
    for step in NICE_STEPS:
        if step >= raw:
            return step
    return math.ceil(raw / 600) * 600


def plan_storyboard(duration_seconds: float, config: StoryboardSettings) -> StoryboardPlan:
    """Frame step and sheet grid for a video: the step grows with the duration so frames stay below max_frames"""
    raw = max(config.min_step_seconds, duration_seconds / config.max_frames)
    step = _nice_step(raw)
    frame_count = max(1, math.ceil(duration_seconds / step))
    per_sheet = config.columns * config.rows
    return StoryboardPlan(
        step_seconds=step,
        frame_count=frame_count,
        columns=config.columns,
        rows=config.rows,
        tile_width=config.tile_width,
        sheet_count=math.ceil(frame_count / per_sheet),
        duration_seconds=duration_seconds,
    )


def client_status(media: MediaFile, config: StoryboardSettings) -> str:
    """StoryboardStatus a client sees for a media row (the row of video_storyboards, or what is expected without one).

    The job status is the one read with the media row (column_property storyboard_job_status).
    """
    return status_for(media, config, media.storyboard_job_status)


def status_for(media: MediaFile, config: StoryboardSettings, job_status: Optional[str]) -> str:
    """client_status for a given job status: the status of a video_storyboards row the caller loaded, None without one"""
    if not media.is_video or not config.enabled:
        return StoryboardStatus.UNSUPPORTED.value
    if job_status:
        return job_status
    # A known short duration comes first: such a video never gets a storyboard, whatever its poster does
    if media.duration_seconds is not None and media.duration_seconds < config.min_duration_seconds:
        return StoryboardStatus.UNSUPPORTED.value
    if media.poster_status == PosterStatus.FAILED.value:
        return StoryboardStatus.FAILED.value
    if media.poster_status != PosterStatus.READY.value:
        return StoryboardStatus.PENDING.value
    # Poster ready: the step needs the duration, reconciliation adds the row for long videos
    if media.duration_seconds is None:
        return StoryboardStatus.UNSUPPORTED.value
    return StoryboardStatus.PENDING.value


def _timestamp(seconds: float) -> str:
    total_ms = round(seconds * 1000)
    hours, rest = divmod(total_ms, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, ms = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{ms:03d}"


LAYOUT_FIELDS = ("step_seconds", "frame_count", "columns", "rows", "tile_width", "tile_height", "sheet_count")


def layout_usable(layout: VideoStoryboard, duration_seconds: Optional[float]) -> bool:
    """True when build_vtt can work: every layout field and the duration are positive numbers"""
    return bool(duration_seconds and duration_seconds > 0) and all(
        (getattr(layout, name) or 0) > 0 for name in LAYOUT_FIELDS
    )


def build_vtt(layout: VideoStoryboard, duration_seconds: float, sheet_url: Callable[[int], str]) -> str:
    """WebVTT body: one cue per frame, pointing at its tile with #xywh"""
    step = layout.step_seconds
    per_sheet = layout.columns * layout.rows
    lines = ["WEBVTT", ""]
    for i in range(layout.frame_count):
        position = i % per_sheet
        x = (position % layout.columns) * layout.tile_width
        y = (position // layout.columns) * layout.tile_height
        start = i * step
        end = min((i + 1) * step, duration_seconds)
        lines.append(f"{_timestamp(start)} --> {_timestamp(end)}")
        lines.append(f"{sheet_url(i // per_sheet)}#xywh={x},{y},{layout.tile_width},{layout.tile_height}")
        lines.append("")
    return "\n".join(lines)


class StoryboardError(Exception):
    """ffmpeg failed, timed out, or wrote no sheet"""


class StoryboardCancelled(StoryboardError):
    """The worker is stopping: ffmpeg was stopped and nothing was written. Not a failure of the video."""


@dataclass(frozen=True)
class StoryboardResult:
    tile_height: int
    sheet_count: int


def video_filter(plan: StoryboardPlan) -> str:
    """Filter chain of both passes: one frame every step, scaled to the tile width, packed into sheets.

    The decoded frames may end long before the duration: keyframes-only decoding stops at the last keyframe
    (with a default GOP, seconds before the end), and a video stream may be shorter than its container. tpad
    repeats the last decoded frame past the duration (fps needs input beyond the last step to emit it) and
    trim stops at the planned frame count, so ffmpeg writes exactly plan.frame_count frames: late tiles repeat
    the last frame and no cue points at the black the tile filter pads the last sheet with.
    """
    pad_seconds = plan.duration_seconds + 2 * plan.step_seconds
    return (
        f"tpad=stop_mode=clone:stop_duration={pad_seconds:.3f},"
        f"fps=1/{plan.step_seconds},trim=end_frame={plan.frame_count},"
        f"scale={plan.tile_width}:-2,tile={plan.columns}x{plan.rows}"
    )


def build_command(
    source: Path,
    out_dir: Path,
    plan: StoryboardPlan,
    config: StoryboardSettings,
    ffmpeg: str,
    os_name: str = os.name,
    nice_binary: Optional[str] = None,
    keyframes_only: bool = True,
) -> List[str]:
    """ffmpeg argument list; on POSIX with nice > 0 it runs under the nice binary (no preexec_fn: the worker uses threads).

    keyframes_only decodes keyframes only (fast); False decodes every frame (the fallback). Both use video_filter.
    """
    command = [
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
        "-threads", str(config.threads),  # decoder
        *(["-skip_frame", "nokey"] if keyframes_only else []),
        "-i", str(source),
        "-an", "-sn", "-dn",
        "-vf", video_filter(plan),
        "-q:v", str(config.jpeg_qscale),
        "-threads", str(config.threads),  # encoder
        "-start_number", "0",
        str(out_dir / "sheet-%03d.jpg"),
    ]
    if os_name == "posix" and config.nice > 0 and nice_binary:
        return [nice_binary, "-n", str(config.nice)] + command
    return command


def _wrote_nothing(returncode: int, stderr: str, sheets: List[Path]) -> bool:
    """True when ffmpeg read the input but no frame reached the output (exit 0 without a sheet, or one of
    NOTHING_WRITTEN_MARKERS), as opposed to failing on the input itself (unreadable, corrupt)"""
    if sheets:
        return False
    return returncode == 0 or any(marker in stderr for marker in NOTHING_WRITTEN_MARKERS)


def _stop(proc: subprocess.Popen) -> None:
    """Ask ffmpeg to stop, kill it when it is still running after STOP_GRACE_SECONDS, and reap it"""
    proc.terminate()
    try:
        proc.communicate(timeout=STOP_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()


def _run_ffmpeg(
    command: List[str], deadline: float, timeout_seconds: int, cancel: Optional[threading.Event]
) -> Tuple[int, bytes]:
    """Run ffmpeg to its end and return (exit code, stderr).

    Stops it at the deadline (time.monotonic(); StoryboardError naming timeout_seconds, the limit of the
    whole extraction) or as soon as cancel is set (StoryboardCancelled). stderr is read while waiting,
    so a chatty ffmpeg never blocks on a full pipe.
    """
    if time.monotonic() >= deadline:
        raise StoryboardError(f"ffmpeg timed out after {timeout_seconds}s")
    try:
        proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except OSError as exc:
        raise StoryboardError(f"cannot run ffmpeg: {exc}") from exc
    try:
        while True:
            try:
                _, stderr = proc.communicate(timeout=POLL_SECONDS)
                return proc.returncode, stderr or b""
            except subprocess.TimeoutExpired:
                pass
            if cancel is not None and cancel.is_set():
                _stop(proc)
                raise StoryboardCancelled("ffmpeg stopped: the worker is shutting down")
            if time.monotonic() >= deadline:
                _stop(proc)
                raise StoryboardError(f"ffmpeg timed out after {timeout_seconds}s")
    except BaseException:
        # Never leave ffmpeg running behind an error (cleanup, then the error goes on)
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        raise


def extract_storyboard(
    source: Path,
    target_dir: Path,
    plan: StoryboardPlan,
    config: StoryboardSettings,
    ffmpeg: str,
    cancel: Optional[threading.Event] = None,
) -> StoryboardResult:
    """Render the sprite sheets of a video into target_dir, atomically.

    The first ffmpeg pass decodes keyframes only; both passes repeat the last decoded frame up to
    plan.duration_seconds (video_filter), so plan.frame_count frames are written. When the first
    pass writes nothing (ffmpeg reached no frame; before the padding, a video with a single
    keyframe), one full-decode pass follows within what is left of the same timeout_seconds;
    any other failure is final. Sheets go
    to a temporary directory next to target_dir, which then replaces it; a failure leaves
    no temporary directory behind. Setting cancel stops ffmpeg within POLL_SECONDS (plus
    STOP_GRACE_SECONDS at most) and raises StoryboardCancelled; target_dir is then left as it was.
    """
    if cancel is not None and cancel.is_set():
        raise StoryboardCancelled("cancelled before ffmpeg started")
    if not source.exists():
        raise StoryboardError(f"source file missing: {source}")
    deadline = time.monotonic() + config.timeout_seconds
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    tmp = target_dir.with_name(f"{target_dir.name}.tmp-{uuid.uuid4().hex[:8]}")
    tmp.mkdir()
    nice_binary = shutil.which("nice")

    def render(keyframes_only: bool) -> Tuple[int, str, List[Path]]:
        command = build_command(source, tmp, plan, config, ffmpeg, nice_binary=nice_binary, keyframes_only=keyframes_only)
        returncode, stderr = _run_ffmpeg(command, deadline, config.timeout_seconds, cancel)
        return returncode, stderr.decode("utf-8", "replace"), sorted(tmp.glob("sheet-*.jpg"))

    try:
        mode = "keyframes only"
        returncode, stderr, sheets = render(keyframes_only=True)
        if _wrote_nothing(returncode, stderr, sheets):
            if cancel is not None and cancel.is_set():
                raise StoryboardCancelled("cancelled between the ffmpeg passes")
            mode = "full decode"
            returncode, stderr, sheets = render(keyframes_only=False)
        if returncode != 0 or not sheets:
            lines = stderr.strip().splitlines()
            detail = lines[-1] if lines else f"exit code {returncode}"
            raise StoryboardError(f"no storyboard written: {detail}")
        if mode == "full decode":
            _logger.info(f"storyboard {source.name}: the keyframes-only pass wrote nothing; {len(sheets)} sheet(s) from a full decode")
        else:
            _logger.debug(f"storyboard {source.name}: {len(sheets)} sheet(s) from a keyframes-only pass")

        from PIL import Image
        with Image.open(sheets[0]) as first:
            tile_height = first.height // plan.rows

        old = None
        if target_dir.exists():
            old = target_dir.with_name(f"{target_dir.name}.old-{uuid.uuid4().hex[:8]}")
            os.replace(target_dir, old)
        try:
            os.replace(tmp, target_dir)
        except OSError:
            if old is not None:
                os.replace(old, target_dir)
            raise
        if old is not None:
            shutil.rmtree(old, ignore_errors=True)
    finally:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
    return StoryboardResult(tile_height=tile_height, sheet_count=len(sheets))
