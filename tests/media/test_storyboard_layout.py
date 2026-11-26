"""Storyboard layout, client status, WebVTT and the choice of extraction pass: no database, no ffmpeg."""
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy.orm.attributes import set_committed_value

from app.backend.config import StoryboardSettings, settings
from app.backend.enums.general import PosterStatus, StoryboardJobStatus, StoryboardStatus
from app.backend.models.media import MediaFile
from app.backend.models.storyboard import VideoStoryboard
from app.backend.services.media import storyboards
from app.backend.services.media.storyboards import (
    StoryboardCancelled,
    StoryboardError,
    build_vtt,
    client_status,
    extract_storyboard,
    plan_storyboard,
)
from app.backend.services.media.urls import storyboard_sheet_url
from tests.media.test_media_thumbnails import decode_source


def test_step_adapts_to_duration():
    config = StoryboardSettings()
    plan = plan_storyboard(60, config)
    assert (plan.step_seconds, plan.frame_count, plan.sheet_count) == (2, 30, 1)
    plan = plan_storyboard(7200, config)
    assert (plan.step_seconds, plan.frame_count, plan.sheet_count) == (30, 240, 3)
    plan = plan_storyboard(36000, config)
    assert (plan.step_seconds, plan.frame_count) == (120, 300)
    assert plan_storyboard(2100, config).step_seconds == 10
    assert plan_storyboard(360000, config).step_seconds == 1200
    plan = plan_storyboard(3, config)
    assert (plan.step_seconds, plan.frame_count) == (2, 2)
    assert (plan.columns, plan.rows, plan.tile_width) == (10, 10, 160)


def _layout() -> VideoStoryboard:
    return VideoStoryboard(
        media_id=1, status=StoryboardJobStatus.READY.value, step_seconds=2, frame_count=250,
        columns=10, rows=10, tile_width=160, tile_height=90, sheet_count=3,
    )


def test_vtt_cues_cross_sheets():
    vtt = build_vtt(_layout(), 499.5, lambda n: f"sheet-{n}")
    assert vtt.startswith("WEBVTT\n\n")
    cues = [block.split("\n") for block in vtt.strip().split("\n\n")[1:]]
    assert len(cues) == 250
    assert cues[0][0] == "00:00:00.000 --> 00:00:02.000"
    assert cues[0][1].endswith("sheet-0#xywh=0,0,160,90")
    assert cues[11][1].endswith("sheet-0#xywh=160,90,160,90")
    assert cues[100][1] == "sheet-1#xywh=0,0,160,90"
    assert cues[-1][0].endswith("--> 00:08:19.500")


def _video(storyboard_job_status=None, **fields) -> MediaFile:
    job_status = storyboard_job_status
    values = dict(media_type="video", poster_status=PosterStatus.READY.value, duration_seconds=120.0)
    values.update(fields)
    media = MediaFile(**values)
    # column_property of the row: a transient instance takes the value as if it were loaded
    set_committed_value(media, "storyboard_job_status", job_status)
    return media


def test_client_status_without_a_row():
    config = StoryboardSettings()
    unsupported = StoryboardStatus.UNSUPPORTED.value
    assert client_status(_video(media_type="image"), config) == unsupported
    assert client_status(_video(duration_seconds=30.0), config) == unsupported
    assert client_status(_video(poster_status=PosterStatus.FAILED.value), config) == StoryboardStatus.FAILED.value
    assert client_status(_video(poster_status=PosterStatus.PENDING.value), config) == StoryboardStatus.PENDING.value
    assert client_status(_video(poster_status=PosterStatus.PROCESSING.value), config) == StoryboardStatus.PENDING.value
    # a known short duration wins over the poster: such a video never gets a storyboard
    assert client_status(_video(duration_seconds=30.0, poster_status=PosterStatus.PENDING.value), config) == unsupported
    assert client_status(_video(duration_seconds=30.0, poster_status=PosterStatus.FAILED.value), config) == unsupported
    assert client_status(_video(duration_seconds=None), config) == unsupported
    assert client_status(_video(), config) == StoryboardStatus.PENDING.value
    assert client_status(_video(), StoryboardSettings(enabled=False)) == unsupported


def test_client_status_with_a_row():
    row = StoryboardJobStatus.PROCESSING.value
    assert client_status(_video(storyboard_job_status=row), StoryboardSettings()) == StoryboardStatus.PROCESSING.value
    ready = _video(storyboard_job_status=StoryboardJobStatus.READY.value)
    assert client_status(ready, StoryboardSettings()) == StoryboardStatus.READY.value
    assert client_status(ready, StoryboardSettings(enabled=False)) == StoryboardStatus.UNSUPPORTED.value


def test_sheet_urls_are_signed_public_media_images(monkeypatch):
    monkeypatch.setattr(settings.storage.imgproxy, "key", "943b421c9eb07c830af81030552c86009268de4e532ba2ee2eab8247c6da0881")
    monkeypatch.setattr(settings.storage.imgproxy, "salt", "520f986b998545b4785e0defbc4f3c1203f22de2374a3d53cb7a7fe9fea309c5")
    expires = datetime(2030, 1, 1, tzinfo=timezone.utc)
    url = storyboard_sheet_url(91, 2, expires)
    assert url.startswith(settings.storage.imgproxy.public_base_url.rstrip("/") + settings.storage.imgproxy.public_prefix + "/")
    assert f"exp:{int(expires.timestamp())}" in url
    assert url.endswith(".jpg")
    assert decode_source(url) == "local:///_derived/storyboards/000/91/sheet-002.jpg"
    assert storyboard_sheet_url(91, 2, expires, base_url="http://host:8080/").startswith(
        "http://host:8080" + settings.storage.imgproxy.public_prefix + "/"
    )


def test_sheet_urls_change_with_each_generation(monkeypatch):
    # nginx caches by URI and the exp: window repeats: sheets regenerated with another layout must not come from the
    # cache of the previous ones (#xywh would miss), so the URL carries imgproxy's cache buster, signed like every option
    monkeypatch.setattr(settings.storage.imgproxy, "key", "943b421c9eb07c830af81030552c86009268de4e532ba2ee2eab8247c6da0881")
    monkeypatch.setattr(settings.storage.imgproxy, "salt", "520f986b998545b4785e0defbc4f3c1203f22de2374a3d53cb7a7fe9fea309c5")
    expires = datetime(2030, 1, 1, tzinfo=timezone.utc)
    generated = datetime(2026, 10, 6, 12, 0, 0)  # naive, as the database returns it: UTC

    url = storyboard_sheet_url(91, 0, expires, generated_at=generated)
    assert f"/cb:{int(generated.replace(tzinfo=timezone.utc).timestamp())}/" in url
    assert url == storyboard_sheet_url(91, 0, expires, generated_at=generated.replace(tzinfo=timezone.utc))
    assert url == storyboard_sheet_url(91, 0, expires, generated_at=generated.replace(microsecond=999)), (
        "whole seconds: MySQL DATETIME keeps no fraction"
    )
    regenerated = storyboard_sheet_url(91, 0, expires, generated_at=generated + timedelta(minutes=5))
    assert regenerated != url
    signature = lambda u: u.split(settings.storage.imgproxy.public_prefix + "/")[1].split("/")[0]  # noqa: E731
    assert signature(regenerated) != signature(url), "the signature covers the cache buster"
    assert "/cb:" not in storyboard_sheet_url(91, 0, expires), "no generation time, no cache buster"


# ------------------------------------------------- extraction passes (ffmpeg stubbed)

# What ffmpeg 7 prints when the keyframe-only pass of a one-keyframe video reaches no frame
NOTHING_WRITTEN = (
    "[vost#0:0/mjpeg] Could not open encoder before EOF\n"
    "[out#0/image2] Nothing was written into output file, because at least one of its streams received no packets."
)


def _stub_ffmpeg(monkeypatch, outcomes, on_call=None):
    """Replace the ffmpeg run; call n returns outcomes[n] = (exit code, stderr, writes a sheet)"""
    calls = []

    def run(command, *args):
        returncode, stderr, writes_sheet = outcomes[len(calls)]
        calls.append((command, args))
        if on_call:
            on_call()
        if writes_sheet:
            from PIL import Image
            Image.new("RGB", (800, 600)).save(Path(command[-1]).parent / "sheet-000.jpg")
        return returncode, stderr.encode()

    monkeypatch.setattr(storyboards, "_run_ffmpeg", run)
    return calls


def _extract(tmp_path: Path, cancel=None):
    source = tmp_path / "video.mp4"
    source.write_bytes(b"x")
    config = StoryboardSettings(columns=5, rows=5)
    target = tmp_path / "storyboards" / "000" / "1"
    return extract_storyboard(source, target, plan_storyboard(4, config), config, "ffmpeg", cancel=cancel), target


@pytest.mark.parametrize(
    "first_pass",
    [(234, NOTHING_WRITTEN, False), (0, "", False)],
    ids=["ffmpeg_reports_nothing_written", "exit_0_without_a_sheet"],
)
def test_a_keyframe_pass_that_writes_nothing_falls_back_to_a_full_decode(tmp_path, monkeypatch, first_pass):
    calls = _stub_ffmpeg(monkeypatch, [first_pass, (0, "", True)])

    result, target = _extract(tmp_path)

    assert len(calls) == 2
    (first, first_args), (second, second_args) = calls
    assert "-skip_frame" in first and "-skip_frame" not in second
    assert first_args == second_args, "the second pass gets what is left of the same time limit, not a new one"
    assert (result.tile_height, result.sheet_count) == (120, 1)
    assert (target / "sheet-000.jpg").is_file()


def test_other_failures_run_ffmpeg_once(tmp_path, monkeypatch):
    calls = _stub_ffmpeg(monkeypatch, [(1, "video.mp4: Invalid data found when processing input", False)])

    with pytest.raises(StoryboardError, match="Invalid data found"):
        _extract(tmp_path)

    assert len(calls) == 1
    assert sorted(p.name for p in (tmp_path / "storyboards" / "000").iterdir()) == [], "no temporary directory left"


def test_a_cancel_between_the_passes_stops(tmp_path, monkeypatch):
    cancel = threading.Event()
    calls = _stub_ffmpeg(monkeypatch, [(234, NOTHING_WRITTEN, False)], on_call=cancel.set)

    with pytest.raises(StoryboardCancelled):
        _extract(tmp_path, cancel=cancel)

    assert len(calls) == 1


def test_a_spent_time_limit_starts_no_ffmpeg():
    # A command that cannot start: reaching Popen would raise "cannot run ffmpeg" instead
    with pytest.raises(StoryboardError, match="timed out after 60s"):
        storyboards._run_ffmpeg(["no-such-ffmpeg-binary"], time.monotonic() - 1, 60, None)
