"""ffmpeg storyboard extraction against a generated test video. Skipped when no ffmpeg binary is available."""
import subprocess
import threading
import time
from pathlib import Path
from typing import List, Optional

import pytest

from app.backend.config import StoryboardSettings
from app.backend.services.media import storyboards
from app.backend.services.media.posters import resolve_binary
from app.backend.services.media.storyboards import (
    StoryboardCancelled,
    StoryboardError,
    build_command,
    extract_storyboard,
    plan_storyboard,
)

FFMPEG = resolve_binary("ffmpeg", imageio_fallback=True)

pytestmark = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not available (install ffmpeg or imageio-ffmpeg)")


def _synth_video(path: Path, seconds: float, keyframe_interval: Optional[int] = 10) -> Path:
    # Default -g 10: a keyframe every second at 10 fps, so -skip_frame nokey still sees every step.
    # -sc_threshold 0: no extra keyframes on scene changes, the interval is exact.
    # None: the encoder's own GOP (libx264: a keyframe every 250 frames, 25 s at 10 fps)
    gop = [] if keyframe_interval is None else ["-g", str(keyframe_interval), "-sc_threshold", "0"]
    subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", f"testsrc=duration={seconds}:size=320x240:rate=10",
         "-pix_fmt", "yuv420p", *gop, str(path)],
        check=True, timeout=120,
    )
    return path


def _tile_luminance(target: Path, plan, tile_height: int) -> List[float]:
    """Mean luminance of every tile of every sheet, in cue order"""
    from PIL import Image, ImageStat
    values = []
    for sheet in sorted(target.glob("sheet-*.jpg")):
        with Image.open(sheet) as img:
            gray = img.convert("L")
            for row in range(plan.rows):
                for column in range(plan.columns):
                    x, y = column * plan.tile_width, row * tile_height
                    values.append(ImageStat.Stat(gray.crop((x, y, x + plan.tile_width, y + tile_height))).mean[0])
    return values


def _leftovers(parent: Path):
    return [p for p in parent.iterdir() if ".tmp-" in p.name]


def test_storyboard_from_test_pattern(tmp_path):
    video = _synth_video(tmp_path / "pattern.mp4", 70)
    target = tmp_path / "storyboards" / "000" / "1"
    config = StoryboardSettings(columns=4, rows=4, timeout_seconds=120)
    plan = plan_storyboard(70, config)
    assert (plan.step_seconds, plan.frame_count, plan.sheet_count) == (2, 35, 3)

    result = extract_storyboard(video, target, plan, config, FFMPEG)

    assert result.tile_height == 120
    assert result.sheet_count == 3
    from PIL import Image
    for i in range(3):
        with Image.open(target / f"sheet-{i:03d}.jpg") as img:
            assert img.size == (640, 480)
    assert not (target / "sheet-003.jpg").exists()
    assert not _leftovers(target.parent)


def test_a_video_with_a_single_keyframe_gets_its_sheet(tmp_path):
    # The e2e upload: 4 s and one keyframe. Without the padding of video_filter the keyframe-only pass gave
    # the fps filter nothing to sample and ffmpeg wrote nothing ("Could not open encoder before EOF"); the
    # full-decode fallback stays for that case (tests/test_storyboard_layout.py)
    video = _synth_video(tmp_path / "one_keyframe.mp4", 4, keyframe_interval=1000)
    target = tmp_path / "storyboards" / "000" / "8"
    config = StoryboardSettings(columns=5, rows=5, timeout_seconds=120)
    plan = plan_storyboard(4, config)
    assert (plan.step_seconds, plan.frame_count, plan.sheet_count) == (2, 2, 1)

    result = extract_storyboard(video, target, plan, config, FFMPEG)

    assert (result.tile_height, result.sheet_count) == (120, 1)
    from PIL import Image
    with Image.open(target / "sheet-000.jpg") as img:
        assert img.size == (5 * 160, 5 * 120)
    assert not _leftovers(target.parent)


# Mean luminance: a frame of the test pattern is far above, the black the tile filter pads a sheet with far below
BLACK_BELOW = 10


@pytest.mark.parametrize(
    "seconds, keyframe_interval",
    [(70, None), (6.1, 1000)],
    ids=["default_gop_70s", "one_keyframe_just_past_a_step"],
)
def test_every_tile_a_cue_points_at_holds_a_frame(tmp_path, seconds, keyframe_interval):
    # Keyframes only: the last keyframe of a default GOP (25 s at 10 fps) comes long before the end, so the fps
    # filter stops there and the tile filter pads with black. The last decoded frame is repeated up to the end
    # instead (repeated tiles are accepted), so ffmpeg writes exactly the planned frames
    video = _synth_video(tmp_path / "gop.mp4", seconds, keyframe_interval)
    target = tmp_path / "storyboards" / "000" / "9"
    config = StoryboardSettings(columns=4, rows=4, timeout_seconds=120)
    plan = plan_storyboard(seconds, config)

    result = extract_storyboard(video, target, plan, config, FFMPEG)

    assert result.sheet_count == plan.sheet_count
    luminance = _tile_luminance(target, plan, result.tile_height)
    cued, padding = luminance[:plan.frame_count], luminance[plan.frame_count:]
    black = [index for index, value in enumerate(cued) if value < BLACK_BELOW]
    assert not black, f"cues {black} of {plan.frame_count} point at black tiles: {[round(v, 1) for v in cued]}"
    assert all(value < BLACK_BELOW for value in padding), "exactly the planned frames: the rest of the last sheet is padding"
    assert not _leftovers(target.parent)


def test_a_broken_source_raises_and_leaves_nothing(tmp_path):
    broken = tmp_path / "broken.mp4"
    broken.write_text("this is not a video")
    target = tmp_path / "storyboards" / "000" / "2"
    config = StoryboardSettings(columns=4, rows=4, timeout_seconds=60)
    plan = plan_storyboard(70, config)

    with pytest.raises(StoryboardError):
        extract_storyboard(broken, target, plan, config, FFMPEG)

    assert not target.exists()
    assert not _leftovers(target.parent)


def test_an_existing_storyboard_is_replaced(tmp_path):
    video = _synth_video(tmp_path / "pattern.mp4", 70)
    target = tmp_path / "storyboards" / "000" / "3"
    target.mkdir(parents=True)
    (target / "old.txt").write_text("old")
    config = StoryboardSettings(columns=4, rows=4, timeout_seconds=120)

    extract_storyboard(video, target, plan_storyboard(70, config), config, FFMPEG)

    assert (target / "sheet-000.jpg").exists()
    assert not (target / "old.txt").exists()
    assert sorted(p.name for p in target.parent.iterdir()) == ["3"]


def test_a_failed_run_keeps_the_old_storyboard(tmp_path):
    broken = tmp_path / "broken.mp4"
    broken.write_text("not a video")
    target = tmp_path / "storyboards" / "000" / "4"
    target.mkdir(parents=True)
    (target / "sheet-000.jpg").write_bytes(b"good")
    config = StoryboardSettings(columns=4, rows=4, timeout_seconds=60)

    with pytest.raises(StoryboardError):
        extract_storyboard(broken, target, plan_storyboard(70, config), config, FFMPEG)

    assert (target / "sheet-000.jpg").read_bytes() == b"good"
    assert sorted(p.name for p in target.parent.iterdir()) == ["4"]


def _endless_command(source, out_dir, plan, config, ffmpeg, **_):
    """A real-time test source that never ends: only a cancel or the timeout stops this ffmpeg"""
    return [
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-re",
        "-f", "lavfi", "-i", "testsrc=size=64x64:rate=5",
        "-vf", "fps=1", "-q:v", "5", "-start_number", "0", str(out_dir / "sheet-%03d.jpg"),
    ]


def _existing_storyboard(tmp_path: Path, name: str) -> Path:
    target = tmp_path / "storyboards" / "000" / name
    target.mkdir(parents=True)
    (target / "sheet-000.jpg").write_bytes(b"good")
    return target


def test_a_cancel_before_the_start_runs_nothing(tmp_path):
    source = tmp_path / "video.mp4"
    source.write_bytes(b"x")
    target = _existing_storyboard(tmp_path, "5")
    config = StoryboardSettings(columns=4, rows=4, timeout_seconds=60)
    cancel = threading.Event()
    cancel.set()

    with pytest.raises(StoryboardCancelled):
        extract_storyboard(source, target, plan_storyboard(70, config), config, FFMPEG, cancel=cancel)

    assert (target / "sheet-000.jpg").read_bytes() == b"good"
    assert sorted(p.name for p in target.parent.iterdir()) == ["5"]


def test_a_cancel_stops_a_running_ffmpeg_and_leaves_nothing(tmp_path, monkeypatch):
    source = tmp_path / "video.mp4"
    source.write_bytes(b"x")
    target = _existing_storyboard(tmp_path, "6")
    config = StoryboardSettings(columns=4, rows=4, timeout_seconds=30)
    cancel = threading.Event()
    real_popen = subprocess.Popen

    def popen_then_cancel(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        cancel.set()  # the worker stops right after ffmpeg started
        return process

    monkeypatch.setattr(storyboards, "build_command", _endless_command)
    monkeypatch.setattr(subprocess, "Popen", popen_then_cancel)
    started = time.monotonic()
    with pytest.raises(StoryboardCancelled):
        extract_storyboard(source, target, plan_storyboard(70, config), config, FFMPEG, cancel=cancel)

    assert time.monotonic() - started < 10, "stopped long before the timeout"
    assert (target / "sheet-000.jpg").read_bytes() == b"good"
    assert sorted(p.name for p in target.parent.iterdir()) == ["6"], "no temporary directory left"


def test_a_timeout_stops_ffmpeg_and_is_a_failure(tmp_path, monkeypatch):
    source = tmp_path / "video.mp4"
    source.write_bytes(b"x")
    target = tmp_path / "storyboards" / "000" / "7"
    config = StoryboardSettings(columns=4, rows=4, timeout_seconds=1)
    monkeypatch.setattr(storyboards, "build_command", _endless_command)

    started = time.monotonic()
    with pytest.raises(StoryboardError, match="timed out") as raised:
        extract_storyboard(source, target, plan_storyboard(70, config), config, FFMPEG, cancel=threading.Event())

    assert not isinstance(raised.value, StoryboardCancelled)
    assert time.monotonic() - started < 10
    assert not target.exists()
    assert not _leftovers(target.parent)


def test_command_threads_and_nice(tmp_path):
    config = StoryboardSettings(columns=4, rows=4, threads=2, nice=10)
    plan = plan_storyboard(70, config)
    posix = build_command(tmp_path / "v.mp4", tmp_path, plan, config, "ffmpeg", os_name="posix", nice_binary="/usr/bin/nice")
    assert posix[:3] == ["/usr/bin/nice", "-n", "10"] and posix[3] == "ffmpeg"
    assert posix.index("-threads") < posix.index("-i"), "decoder threads"
    assert posix.count("-threads") == 2 and posix[posix.index("-threads") + 1] == "2"
    windows = build_command(tmp_path / "v.mp4", tmp_path, plan, config, "ffmpeg", os_name="nt", nice_binary="/usr/bin/nice")
    assert windows[0] == "ffmpeg"
    assert windows[windows.index("-skip_frame") + 1] == "nokey" and windows.index("-skip_frame") < windows.index("-i")
    full = build_command(tmp_path / "v.mp4", tmp_path, plan, config, "ffmpeg", os_name="nt", keyframes_only=False)
    assert "-skip_frame" not in full and full[full.index("-vf") + 1] == windows[windows.index("-vf") + 1]
    no_nice = build_command(tmp_path / "v.mp4", tmp_path, plan, config, "ffmpeg", os_name="posix", nice_binary=None)
    assert no_nice[0] == "ffmpeg"


@pytest.mark.parametrize("keyframes_only", [True, False], ids=["keyframes_only", "full_decode"])
def test_command_repeats_the_last_frame_and_cuts_at_the_planned_count(tmp_path, keyframes_only):
    config = StoryboardSettings(columns=4, rows=4)
    plan = plan_storyboard(70.5, config)
    command = build_command(tmp_path / "v.mp4", tmp_path, plan, config, "ffmpeg", os_name="nt", keyframes_only=keyframes_only)
    pad, fps, trim, scale, tile = command[command.index("-vf") + 1].split(",")
    # The last decoded frame is cloned past the end (the fps filter needs input beyond the last step to emit it) ...
    assert pad.startswith("tpad=stop_mode=clone:stop_duration=")
    assert float(pad.split("stop_duration=")[1]) >= plan.duration_seconds + plan.step_seconds
    assert fps == "fps=1/2"
    # ... and the output stops at the planned frame count, so every cue points at a written frame
    assert trim == f"trim=end_frame={plan.frame_count}" == "trim=end_frame=36"
    assert (scale, tile) == ("scale=160:-2", "tile=4x4")
