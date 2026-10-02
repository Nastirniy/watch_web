"""Codec and duration probing: RFC 6381 strings, ffmpeg banner parsing, a real probe, and the API fields."""
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
