"""Codec and duration probing: RFC 6381 strings, ffmpeg banner parsing, a real probe, and the API fields."""
import json
import subprocess
from pathlib import Path

import pytest

from app.backend.enums.routes import MediaRoutes
from app.backend.api.v1 import V1
from app.backend.services.media import probe
from app.backend.services.media.posters import resolve_binary
from tests.helpers import make_post, make_user, make_dictionary, make_observation, make_media

FFMPEG = resolve_binary("ffmpeg", imageio_fallback=True)
FFPROBE = resolve_binary("ffprobe")

BANNER = """Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'clip.mp4':
  Duration: 00:01:02.50, start: 0.000000, bitrate: 1200 kb/s
  Stream #0:0[0x1](und): Video: h264 (High) (avc1 / 0x31637661), yuv420p(progressive), 1920x1080 [SAR 1:1 DAR 16:9], 1064 kb/s, 25 fps
  Stream #0:1[0x2](und): Audio: aac (LC) (mp4a / 0x6134706D), 48000 Hz, stereo, fltp, 128 kb/s
At least one output file must be specified"""


def test_codec_strings():
    assert probe.video_codec_string("h264", "High", 31) == "avc1.64001F"
    assert probe.video_codec_string("h264", "Constrained Baseline", 30) == "avc1.42E01E"
    assert probe.video_codec_string("h264", "Main", None) == "avc1.4D001F"
    assert probe.video_codec_string("hevc", "Main", 93) == "hvc1.1.6.L93.B0"
    assert probe.video_codec_string("vp9", "Profile 0", None) == "vp9"
    assert probe.video_codec_string("av1", None, None) == "av01.0.04M.08"
    assert probe.video_codec_string(None, None, None) is None
    assert probe.audio_codec_string("aac", "LC") == "mp4a.40.2"
    assert probe.audio_codec_string("aac", "HE-AAC") == "mp4a.40.5"
    assert probe.audio_codec_string("opus", None) == "opus"
    assert probe.audio_codec_string("mp3", None) == "mp4a.40.34"


def test_playback_type_string():
    result = probe.ProbeResult(video_codec="h264", video_profile="High", video_level=40, audio_codec="aac", audio_profile="LC")
    assert probe.playback_type("video/mp4", result) == 'video/mp4; codecs="avc1.640028, mp4a.40.2"'
    audio_only = probe.ProbeResult(audio_codec="opus")
    assert probe.playback_type("audio/ogg; charset=binary", audio_only) == 'audio/ogg; codecs="opus"'
    assert probe.playback_type("video/webm", probe.ProbeResult()) == "video/webm"


def test_banner_parsing(monkeypatch):
    class Proc:
        returncode = 1
        stdout = b""
        stderr = BANNER.encode()

    monkeypatch.setattr(probe, "_run", lambda command, timeout: Proc())
    result = probe._probe_banner("ffmpeg", Path("clip.mp4"), 5)
    assert result.duration_seconds == 62.5
    assert (result.video_codec, result.video_profile, result.width, result.height) == ("h264", "High", 1920, 1080)
    assert (result.audio_codec, result.audio_profile) == ("aac", "LC")
    assert probe.playback_type("video/mp4", result) == 'video/mp4; codecs="avc1.64001F, mp4a.40.2"'


def _json_probe(monkeypatch, stream: dict) -> probe.ProbeResult:
    class Proc:
        returncode = 0
        stderr = b""
        stdout = json.dumps({"format": {"duration": "2.0"}, "streams": [stream]}).encode()

    monkeypatch.setattr(probe, "_run", lambda command, timeout: Proc())
    return probe._probe_json("ffprobe", Path("clip.mp4"), 5)


@pytest.mark.parametrize("rotation, size", [(90, (360, 640)), (-90, (360, 640)), (270, (360, 640)), (180, (640, 360)), (0, (640, 360))])
def test_json_probe_reports_the_displayed_size(monkeypatch, rotation, size):
    """A phone records a vertical video as a landscape frame plus a display matrix; clients size the player by width/height"""
    stream = {"codec_type": "video", "codec_name": "h264", "width": 640, "height": 360, "side_data_list": [{"rotation": rotation}]}
    result = _json_probe(monkeypatch, stream)
    assert (result.width, result.height) == size


def test_json_probe_reads_the_legacy_rotate_tag(monkeypatch):
    """ffprobe before 5.0 reported the rotation as a stream tag"""
    stream = {"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080, "tags": {"rotate": "90"}}
    result = _json_probe(monkeypatch, stream)
    assert (result.width, result.height) == (1080, 1920)


def test_banner_probe_reports_the_displayed_size(monkeypatch):
    rotated = BANNER.replace(
        "Stream #0:1[0x2]",
        "    Side data:\n      displaymatrix: rotation of -90.00 degrees\n  Stream #0:1[0x2]",
    )

    class Proc:
        returncode = 1
        stdout = b""
        stderr = rotated.encode()

    monkeypatch.setattr(probe, "_run", lambda command, timeout: Proc())
    result = probe._probe_banner("ffmpeg", Path("clip.mp4"), 5)
    assert (result.width, result.height) == (1080, 1920)


def test_no_video_stream_needs_a_probe_that_read_the_file():
    """Only a probe that found another stream proves the picture is missing; an empty result proves nothing"""
    assert probe.has_no_video_stream({"video_codec": None, "audio_codec": "aac", "duration_seconds": 12.0})
    assert not probe.has_no_video_stream({"video_codec": "h264", "audio_codec": "aac"})
    assert not probe.has_no_video_stream({}), "the probe failed: nothing is known"
    assert not probe.has_no_video_stream({"video_codec": None, "audio_codec": None, "duration_seconds": None})


def _jpeg(path: Path, size, orientation=None) -> Path:
    from PIL import Image
    exif = Image.Exif()
    if orientation:
        exif[0x0112] = orientation
    Image.new("RGB", size, (200, 30, 30)).save(path, format="JPEG", exif=exif.tobytes())
    return path


@pytest.mark.parametrize("orientation, size", [(None, (40, 30)), (1, (40, 30)), (3, (40, 30)), (6, (30, 40)), (8, (30, 40)), (5, (30, 40))])
def test_image_dimensions_are_the_displayed_size(tmp_path, orientation, size):
    """EXIF orientation 5-8 turns the stored frame by 90 degrees; browsers and imgproxy show it turned"""
    assert probe.image_dimensions(_jpeg(tmp_path / "photo.jpg", (40, 30), orientation)) == size


def test_image_dimensions_of_an_unreadable_file(tmp_path):
    broken = tmp_path / "broken.jpg"
    broken.write_bytes(b"\xff\xd8\xff\xe0 not really a jpeg")
    assert probe.image_dimensions(broken) is None
    assert probe.image_dimensions(tmp_path / "missing.jpg") is None


@pytest.mark.asyncio
async def test_probe_missing_all_refreshes_the_size_of_existing_rows(db, session_maker, monkeypatch, tmp_path, capsys):
    """Rows stored before sizes were reported as displayed keep the stored frame size until probed again"""
    from app.backend import database, media_tools

    async def maker():
        return session_maker

    async def no_close():
        return None

    monkeypatch.setattr(database, "get_session_maker", maker)
    monkeypatch.setattr(database, "close_db", no_close)
    monkeypatch.setattr(media_tools, "_storage_root", lambda: tmp_path)
    monkeypatch.setattr(probe, "resolve_tools", lambda: ("ffmpeg", None))
    monkeypatch.setattr(probe, "metadata_for", lambda source, mime_type, ffmpeg, ffprobe: {
        "duration_seconds": 12.0, "video_codec": "h264", "audio_codec": "aac",
        "playback_type": 'video/mp4; codecs="avc1.64001F, mp4a.40.2"', "width": 360, "height": 640,
    })
    post = await make_post(db)
    ids = await make_dictionary(db)
    user = await make_user(db, post_id=post.id)
    observation = await make_observation(db, user, post, ids["signal_type_id"])
    video = await make_media(db, observation, user, media_type="video", width=640, height=360, duration_seconds=12.0,
                             playback_type='video/mp4; codecs="avc1.64001F, mp4a.40.2"')
    photo = await make_media(db, observation, user, width=40, height=30)
    (tmp_path / photo.file_path).parent.mkdir(parents=True, exist_ok=True)
    _jpeg(tmp_path / photo.file_path, (40, 30), orientation=6)

    assert await media_tools.probe_missing(limit=100) == 0
    await db.refresh(video)
    await db.refresh(photo)
    assert (video.width, video.height) == (640, 360) and (photo.width, photo.height) == (40, 30), "only missing metadata"

    assert await media_tools.probe_missing(limit=100, refresh=True) == 0
    await db.refresh(video)
    await db.refresh(photo)
    assert (video.width, video.height) == (360, 640)
    assert (photo.width, photo.height) == (30, 40)
    assert "probed 2 file(s), 0 failed" in capsys.readouterr().out


@pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not available")
def test_real_probe_of_a_rotated_and_an_audio_only_file(tmp_path):
    plain, rotated, audio_only = tmp_path / "plain.mp4", tmp_path / "rotated.mp4", tmp_path / "audio.mp4"
    run = lambda *args: subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *args], check=True, timeout=120)
    run("-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=10", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(plain))
    run("-display_rotation", "90", "-i", str(plain), "-c", "copy", str(rotated))
    run("-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:a", "aac", str(audio_only))

    values = probe.metadata_for(rotated, "video/mp4", FFMPEG, FFPROBE)
    assert (values["width"], values["height"]) == (240, 320)
    values = probe.metadata_for(audio_only, "video/mp4", FFMPEG, FFPROBE)
    assert values["video_codec"] is None and values["audio_codec"] == "aac"
    assert probe.has_no_video_stream(values)


@pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not available")
def test_real_probe_of_generated_video(tmp_path):
    video = tmp_path / "clip.mp4"
    subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=duration=3:size=320x240:rate=10",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
         "-c:v", "libx264", "-c:a", "aac", "-pix_fmt", "yuv420p", "-shortest", str(video)],
        check=True, timeout=120,
    )
    values = probe.metadata_for(video, "video/mp4", FFMPEG, FFPROBE)
    assert values["video_codec"] == "h264" and values["audio_codec"] == "aac"
    assert 2.5 <= values["duration_seconds"] <= 3.5
    assert values["width"] == 320 and values["height"] == 240
    assert values["playback_type"].startswith('video/mp4; codecs="avc1.') and "mp4a.40.2" in values["playback_type"]
    assert probe.metadata_for(tmp_path / "missing.mp4", "video/mp4", FFMPEG, FFPROBE) == {}


@pytest.mark.asyncio
async def test_media_info_carries_playback_fields(client, db, headers):
    post = await make_post(db)
    ids = await make_dictionary(db)
    user = await make_user(db, perms=["media.view"], post_id=post.id)
    observation = await make_observation(db, user, post, ids["signal_type_id"])
    probed = await make_media(db, observation, user, media_type="video", duration_seconds=12.5, video_codec="h264",
                              audio_codec="aac", playback_type='video/mp4; codecs="avc1.64001F, mp4a.40.2"', width=1280, height=720)
    unprobed = await make_media(db, observation, user, media_type="audio")
    image = await make_media(db, observation, user)

    url = V1.path(MediaRoutes.MEDIA_INFO)
    body = (await client.get(url.format(media_id=probed.id), headers=headers(user))).json()
    assert body["duration_seconds"] == 12.5 and body["video_codec"] == "h264" and body["audio_codec"] == "aac"
    assert body["playback_type"] == 'video/mp4; codecs="avc1.64001F, mp4a.40.2"' and body["width"] == 1280
    body = (await client.get(url.format(media_id=unprobed.id), headers=headers(user))).json()
    assert body["playback_type"] == "audio/mpeg" and body["duration_seconds"] is None, "bare mime type until probed"
    body = (await client.get(url.format(media_id=image.id), headers=headers(user))).json()
    assert body["playback_type"] is None
