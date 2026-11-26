"""ffmpeg poster extraction against a generated test video. Skipped when no ffmpeg binary is available."""
import subprocess
from pathlib import Path

import pytest

from app.backend.config import PosterSettings
from app.backend.services.media.posters import PosterError, is_near_black, make_poster, probe_duration, resolve_binary

FFMPEG = resolve_binary("ffmpeg", imageio_fallback=True)
FFPROBE = resolve_binary("ffprobe")

pytestmark = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not available (install ffmpeg or imageio-ffmpeg)")


def _synth_video(path: Path, source: str, seconds: int = 3) -> Path:
    # lavfi: "testsrc=duration=..." for a bare source, "color=c=black:duration=..." when options are already present
    separator = ":" if "=" in source else "="
    subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"{source}{separator}duration={seconds}:size=320x240:rate=10",
         "-pix_fmt", "yuv420p", str(path)],
        check=True, timeout=60,
    )
    return path


def test_poster_from_test_pattern(tmp_path):
    video = _synth_video(tmp_path / "pattern.mp4", "testsrc")
    target = tmp_path / "posters" / "000" / "1.jpg"
    config = PosterSettings(timeout_seconds=30, max_width=160, frame_position=0.5)

    result = make_poster(video, target, config, FFMPEG, FFPROBE)

    assert target.exists() and target.stat().st_size > 0
    assert not list(target.parent.glob("*.tmp-*")), "temp files are cleaned up"
    assert result.duration_seconds is not None and 2.5 <= result.duration_seconds <= 3.5
    assert result.seek_seconds == pytest.approx(result.duration_seconds * 0.5, abs=0.05)
    from PIL import Image
    with Image.open(target) as img:
        assert img.width == 160 and img.format == "JPEG"
    assert not is_near_black(target)


def test_black_video_triggers_one_retry(tmp_path):
    video = _synth_video(tmp_path / "black.mp4", "color=black", seconds=6)
    target = tmp_path / "black.jpg"
    result = make_poster(video, target, PosterSettings(timeout_seconds=30, max_width=160), FFMPEG, FFPROBE)
    assert target.exists()
    assert result.retried_black is True
    assert is_near_black(target), "still black after the retry, but a poster exists"


def test_missing_source_and_duration_probe(tmp_path):
    with pytest.raises(PosterError):
        make_poster(tmp_path / "nope.mp4", tmp_path / "x.jpg", PosterSettings(), FFMPEG, FFPROBE)
    video = _synth_video(tmp_path / "short.mp4", "testsrc", seconds=2)
    duration = probe_duration(video, FFMPEG, None)
    assert duration is not None and 1.5 <= duration <= 2.5, "ffmpeg banner fallback works without ffprobe"
