"""Container and codec metadata for video and audio files, and the playback type a browser can test.

probe_media() reads only stream headers (ffprobe, or the ffmpeg banner when
ffprobe is not installed, which is the case with the imageio-ffmpeg fallback
on Windows). The result feeds MediaFile.duration_seconds, width, height,
video_codec, audio_codec and playback_type.

playback_type is the RFC 6381 string a page hands to HTMLMediaElement
.canPlayType(), for example 'video/mp4; codecs="avc1.64001F, mp4a.40.2"'.
It is best effort: when a codec has no known mapping the string carries what
is known, and the client falls back to a poster plus download when
canPlayType() answers "" for it.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from app.backend.config import settings
from app.backend.services.media.posters import PosterError, _run, resolve_binary

PROBE_TIMEOUT_SECONDS = 20

# ffmpeg banner lines, used when ffprobe is unavailable
_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_VIDEO_RE = re.compile(r"Stream #\d+:\d+[^:]*: Video: (\w+)(?: \(([^)]*)\))?.*?(\d{2,5})x(\d{2,5})")
_AUDIO_RE = re.compile(r"Stream #\d+:\d+[^:]*: Audio: (\w+)(?: \(([^)]*)\))?")

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
    base = (mime_type or "").split(";", 1)[0].strip().lower()
    return f'{base}; codecs="{codecs}"' if codecs else base


# ---- probing -----------------------------------------------------------------

def _probe_json(ffprobe: str, source: Path, timeout: float) -> ProbeResult:
    proc = _run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration:stream=codec_type,codec_name,profile,level,width,height",
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
            result.width = stream.get("width") or None
            result.height = stream.get("height") or None
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
        result.width, result.height = int(video.group(3)), int(video.group(4))
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
