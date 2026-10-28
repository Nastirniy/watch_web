"""Container and codec metadata for video and audio files, and the playback type a browser can test.

probe_media() reads only stream headers (ffprobe, or the ffmpeg banner when
ffprobe is not installed, which is the case with the imageio-ffmpeg fallback
on Windows). The result feeds MediaFile.duration_seconds, width, height,
video_codec, audio_codec and playback_type. width and height are the size as
displayed: a phone stores a vertical video as a landscape frame plus a
rotation, and players (like ffmpeg for posters and storyboards) show it turned.
image_dimensions() does the same for images (EXIF orientation).

playback_type is the RFC 6381 string a page hands to HTMLMediaElement
.canPlayType(), for example 'video/mp4; codecs="avc1.64001F, mp4a.40.2"'.
It is best effort: when a codec has no known mapping the string carries what
is known, and the client falls back to a poster plus download when
canPlayType() answers "" for it.
"""
from __future__ import annotations

import json
import re
import struct
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Tuple

from app.backend.config import settings
from app.backend.utils.content_type import parse_content_type
from app.backend.services.media.posters import PosterError, _run, resolve_binary

PROBE_TIMEOUT_SECONDS = 20

# ffmpeg banner lines, used when ffprobe is unavailable
_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_VIDEO_RE = re.compile(r"Stream #\d+:\d+[^:]*: Video: (\w+)(?: \(([^)]*)\))?.*?(\d{2,5})x(\d{2,5})")
_AUDIO_RE = re.compile(r"Stream #\d+:\d+[^:]*: Audio: (\w+)(?: \(([^)]*)\))?")
_ROTATION_RE = re.compile(r"displaymatrix: rotation of (-?\d+(?:\.\d+)?) degrees")

# EXIF Orientation values that turn the stored frame by 90 degrees (5-8: transpose, 90 CW, transverse, 90 CCW)
_EXIF_ORIENTATION = 0x0112
_EXIF_QUARTER_TURNS = {5, 6, 7, 8}

# H.264 profile_idc by ffprobe profile name (RFC 6381 avc1.PPCCLL)
_H264_PROFILES = {
    "baseline": "42", "constrained baseline": "42", "main": "4D", "extended": "58",
    "high": "64", "high 10": "6E", "high 4:2:2": "7A", "high 4:4:4 predictive": "F4", "high 4:4:4": "F4",
}
# HEVC general_profile_idc by ffprobe profile name (hvc1.P.C.LX.B0)
_HEVC_PROFILES = {"main": "1", "main 10": "2", "main still picture": "3", "rext": "4"}
# AAC object type by ffprobe profile name (mp4a.40.OT)
_AAC_PROFILES = {"lc": "2", "he-aac": "5", "he-aacv2": "29", "main": "1", "ssr": "3", "ltp": "4"}

_AUDIO_SIMPLE = {"opus": "opus", "vorbis": "vorbis", "flac": "flac", "mp3": "mp4a.40.34"}
_VIDEO_SIMPLE = {"vp8": "vp8", "vp9": "vp9", "av1": "av01.0.04M.08", "theora": "theora"}


@dataclass
class ProbeResult:
    duration_seconds: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None
    video_codec: Optional[str] = None
    audio_codec: Optional[str] = None
    video_profile: Optional[str] = None
    video_level: Optional[int] = None
    audio_profile: Optional[str] = None


def resolve_tools() -> Tuple[Optional[str], Optional[str]]:
    """(ffmpeg, ffprobe) absolute paths from settings; either may be None"""
    return (
        resolve_binary(settings.storage.ffmpeg_path, imageio_fallback=True),
        resolve_binary(settings.storage.ffprobe_path),
    )


# ---- codec strings -----------------------------------------------------------

def video_codec_string(codec: Optional[str], profile: Optional[str], level: Optional[int]) -> Optional[str]:
    if not codec:
        return None
    codec = codec.lower()
    profile_key = (profile or "").lower().strip()
    if codec == "h264":
        idc = _H264_PROFILES.get(profile_key, "64")
        constraints = "E0" if profile_key == "constrained baseline" else "00"
        lvl = f"{level:02X}" if level and level > 0 else "1F"
        return f"avc1.{idc}{constraints}{lvl}"
    if codec in ("hevc", "h265"):
        idc = _HEVC_PROFILES.get(profile_key, "1")
        lvl = level if level and level > 0 else 93
        return f"hvc1.{idc}.6.L{lvl}.B0"
    return _VIDEO_SIMPLE.get(codec, codec)


def audio_codec_string(codec: Optional[str], profile: Optional[str]) -> Optional[str]:
    if not codec:
        return None
    codec = codec.lower()
    if codec == "aac":
        return f"mp4a.40.{_AAC_PROFILES.get((profile or '').lower().strip(), '2')}"
    return _AUDIO_SIMPLE.get(codec, codec)


def playback_type(mime_type: str, result: ProbeResult) -> str:
    """'video/mp4; codecs="avc1.64001F, mp4a.40.2"' or the bare MIME type when nothing is known"""
    parts = [
        video_codec_string(result.video_codec, result.video_profile, result.video_level),
        audio_codec_string(result.audio_codec, result.audio_profile),
    ]
    codecs = ", ".join(p for p in parts if p)
    parsed = parse_content_type(mime_type)
    if parsed is None:
        return ""
    # quoted always, as RFC 6381 writes it (codec strings hold no quote or backslash)
    return f'{parsed.essence}; codecs="{codecs}"' if codecs else parsed.essence


# ---- probing -----------------------------------------------------------------

def _displayed(width: Optional[int], height: Optional[int], rotation: Any) -> Tuple[Optional[int], Optional[int]]:
    """Stored frame size turned by the display rotation: a quarter turn swaps width and height"""
    try:
        quarter_turn = int(round(float(rotation))) % 180 == 90
    except (TypeError, ValueError):
        quarter_turn = False
    return (height, width) if quarter_turn else (width, height)


def _stream_rotation(stream: dict) -> Any:
    """Display matrix rotation (ffprobe 5 and later), else the legacy 'rotate' tag"""
    for side_data in stream.get("side_data_list") or []:
        if "rotation" in side_data:
            return side_data["rotation"]
    return (stream.get("tags") or {}).get("rotate")


def _probe_json(ffprobe: str, source: Path, timeout: float) -> ProbeResult:
    proc = _run(
        [ffprobe, "-v", "error", "-show_entries",
         "format=duration:stream=codec_type,codec_name,profile,level,width,height:stream_side_data=rotation:stream_tags=rotate",
         "-of", "json", str(source)],
        timeout,
    )
    if proc.returncode != 0:
        raise PosterError(f"ffprobe failed: {proc.stderr.decode('utf-8', 'replace').strip()[-200:]}")
    data = json.loads(proc.stdout.decode("utf-8", "replace") or "{}")
    result = ProbeResult()
    try:
        duration = float(data.get("format", {}).get("duration") or 0)
        result.duration_seconds = round(duration, 3) if duration > 0 else None
    except (TypeError, ValueError):
        pass
    for stream in data.get("streams", []):
        kind = stream.get("codec_type")
        if kind == "video" and result.video_codec is None:
            result.video_codec = stream.get("codec_name")
            result.video_profile = stream.get("profile")
            level = stream.get("level")
            result.video_level = int(level) if isinstance(level, int) and level > 0 else None
            result.width, result.height = _displayed(stream.get("width") or None, stream.get("height") or None,
                                                     _stream_rotation(stream))
        elif kind == "audio" and result.audio_codec is None:
            result.audio_codec = stream.get("codec_name")
            result.audio_profile = stream.get("profile")
    return result


def _probe_banner(ffmpeg: str, source: Path, timeout: float) -> ProbeResult:
    """Parse 'ffmpeg -i' output; no level information, used only without ffprobe"""
    proc = _run([ffmpeg, "-hide_banner", "-nostdin", "-i", str(source)], timeout)
    text = proc.stderr.decode("utf-8", "replace")
    result = ProbeResult()
    match = _DURATION_RE.search(text)
    if match:
        hours, minutes, seconds = match.groups()
        value = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        result.duration_seconds = round(value, 3) if value > 0 else None
    video = _VIDEO_RE.search(text)
    if video:
        result.video_codec, result.video_profile = video.group(1), video.group(2)
        # The side data of the video stream sits between its line and the next stream
        block = text[video.end():].split("Stream #", 1)[0]
        rotation = _ROTATION_RE.search(block)
        result.width, result.height = _displayed(int(video.group(3)), int(video.group(4)),
                                                 rotation.group(1) if rotation else None)
    audio = _AUDIO_RE.search(text)
    if audio:
        result.audio_codec, result.audio_profile = audio.group(1), audio.group(2)
    if result.duration_seconds is None and not video and not audio:
        raise PosterError("ffmpeg could not read the file")
    return result


def probe_media(source: Path, ffmpeg: Optional[str], ffprobe: Optional[str], timeout: float = PROBE_TIMEOUT_SECONDS) -> ProbeResult:
    """Header-only probe. Raises PosterError when neither tool can read the file."""
    if not source.exists():
        raise PosterError(f"source file missing: {source}")
    if ffprobe:
        return _probe_json(ffprobe, source, timeout)
    if ffmpeg:
        return _probe_banner(ffmpeg, source, timeout)
    raise PosterError("neither ffprobe nor ffmpeg is available")


def metadata_for(source: Path, mime_type: str, ffmpeg: Optional[str], ffprobe: Optional[str]) -> dict:
    """Column values for a MediaFile row (empty when the probe fails)"""
    try:
        result = probe_media(source, ffmpeg, ffprobe)
    except PosterError:
        return {}
    values = {
        "duration_seconds": result.duration_seconds,
        "video_codec": result.video_codec,
        "audio_codec": result.audio_codec,
        "playback_type": playback_type(mime_type, result),
    }
    if result.width and result.height:
        values["width"], values["height"] = result.width, result.height
    return values


def has_no_video_stream(values: Mapping[str, Any]) -> bool:
    """True when a probe read the file and found an audio stream but no video stream: no poster can ever exist.

    An empty or blank result proves nothing (the probe failed or found no stream at all): the poster job then
    tries, and fails, as for any unreadable file.
    """
    return values.get("video_codec") is None and values.get("audio_codec") is not None


# Pillow's decompression bomb limit guards decoding; reading a header decodes nothing, and a 200 MP phone photo
# must report its size (to be compared with imgproxy's limit) instead of failing
_HEADER_LOCK = threading.Lock()


def image_dimensions(source: Path) -> Optional[Tuple[int, int]]:
    """Width and height of an image as displayed, from its header only (no pixel data is decoded), or None.

    EXIF orientation 5-8 swaps them. Only EXIF found while the header is parsed is used: Pillow's getexif() would
    decode a whole PNG to find a late eXIf chunk.
    """
    from PIL import Image

    try:
        with _HEADER_LOCK:
            limit, Image.MAX_IMAGE_PIXELS = Image.MAX_IMAGE_PIXELS, None
            try:
                with Image.open(source) as img:
                    width, height = img.size
                    exif_block = img.info.get("exif")
            finally:
                Image.MAX_IMAGE_PIXELS = limit
    except OSError:      # missing, unreadable or not an image (UnidentifiedImageError)
        return None
    orientation = None
    if exif_block:
        try:
            exif = Image.Exif()
            exif.load(exif_block)
            orientation = exif.get(_EXIF_ORIENTATION)
        except (SyntaxError, ValueError, struct.error):     # a malformed EXIF block: the stored size stands
            orientation = None
    return (height, width) if orientation in _EXIF_QUARTER_TURNS else (width, height)
