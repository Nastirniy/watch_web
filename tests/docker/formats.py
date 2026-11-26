"""Format matrix against the compose.test.yml stack: every accepted image, video and audio format through nginx.

    docker build -t watch-web .
    docker compose -f tests/docker/compose.test.yml up -d
    venv/Scripts/python tests/docker/formats.py
    docker compose -f tests/docker/compose.test.yml down -v

Images (JPEG baseline / progressive / EXIF-rotated / CMYK / grayscale / 64 MP / 200 MP, PNG RGB / alpha / palette /
16 bit, WebP lossy / lossless / animated, GIF static / animated, tiny, portrait, above imgproxy's 250 MP limit):
every thumbnail size renders, fits its box without enlarging and keeps the displayed orientation; width and height
are the displayed size; the image above the limit is stored but unsupported (image_too_large_for_thumbnails).

Videos (MP4 H.264 / H.265 8 and 10 bit / MPEG-4, M4V, WebM VP8 / VP9 / AV1, MOV H.264 / ProRes, MKV H.264 / HEVC /
VP9, no audio, a 90 degree display rotation, a really odd size, 4K, one keyframe in 30 s, 230 s with two sheets,
0.5 s): probe, poster, storyboard (WebVTT, sheets through /media-images/ without a token, sheet size = the #xywh
grid, one cue per step, no cue on a black tile), statuses. A video file with only audio is unsupported at once
(no_video_stream), a truncated one fails after its attempts (poster_failed). /stream through nginx's auth cache:
bytes, the API's headers on a miss and a hit, refusals as the API's problems, logout and archive stop a stream within
the 30 s of the cache. Audio (MP3, WAV, FLAC, Ogg Vorbis, Opus, M4A) and video: accepted by the type the sniffer
names (libmagic in the api image, one of the format's content type and aliases), stored and streamed under the
format's canonical name (video/matroska for an MKV libmagic calls video/x-matroska, audio/wav for audio/x-wav). Bytes
the sniffer cannot name, or names otherwise, are refused whatever the extension says (415
media_content_type_not_accepted). Document formats are checked by
tests/media/test_file_sniff.py run inside the api image (the command is in its docstring): no observation route takes a
document.
Exit code 0 when every check passes.
Needs ffmpeg on the host (imageio-ffmpeg) and Pillow; the stack's worker runs with a short retry backoff.
"""
from __future__ import annotations

import base64
import hashlib
import io
import math
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

import requests
from PIL import Image, ImageDraw, ImageStat

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.backend.enums.routes import AuthRoutes, MediaRoutes  # noqa: E402

Image.MAX_IMAGE_PIXELS = None     # this script builds and opens images above Pillow's decompression bomb limit

NGINX = os.environ.get("E2E_NGINX", f"http://127.0.0.1:{os.environ.get('E2E_NGINX_PORT', '18089')}")
API = f"{NGINX}/api/v1"
LOGIN = {
    "certificate_cn": "admin@radiomonitoring.local",
    "certificate_serial": "ABC123DEF456",
    "certificate_fingerprint": "12:34:56:78:90:AB:CD:EF:12:34:56:78:90:AB:CD:EF:12:34:56:78",
}
BOXES = {"small": 150, "medium": 300, "large": 600, "preview": 1600}   # storage.thumbnail defaults
POSTER_MAX_SIDE = 1920                                                  # storage.poster.max_width
GRID = (10, 10)                                                         # storage.storyboard columns x rows
TILE_WIDTH = 160
WAIT_SECONDS = 480

failures: list[str] = []
summary: dict[str, list[str]] = {}


def check(name: str, ok: bool, label: str, detail: str = "") -> bool:
    print(f"[{'ok  ' if ok else 'FAIL'}] {name}: {label}{(': ' + detail) if detail else ''}", flush=True)
    summary.setdefault(name, []).append(("ok " if ok else "FAIL ") + label)
    if not ok:
        failures.append(f"{name}: {label} {detail}")
    return ok


def note(name: str, text: str) -> None:
    print(f"[info] {name}: {text}", flush=True)


def fit(display: tuple[int, int], box: int) -> tuple[int, int]:
    """Size of an image fitted into a square box without enlarging"""
    width, height = display
    scale = min(1.0, box / max(width, height))
    return round(width * scale), round(height * scale)


def close_to(got: tuple[int, int], want: tuple[int, int], slack: int) -> bool:
    return abs(got[0] - want[0]) <= slack and abs(got[1] - want[1]) <= slack


# --------------------------------------------------------------------------- images

def pattern(width: int, height: int) -> Image.Image:
    img = Image.new("RGB", (width, height), (30, 90, 140))
    draw = ImageDraw.Draw(img)
    step = max(width // 20, 2)
    for x in range(0, width, step):
        draw.rectangle([x, 0, x + step // 2, height], fill=(220, 180, 60))
    draw.rectangle([0, 0, width // 4, height // 4], fill=(200, 30, 30))
    return img


def encode(img: Image.Image, fmt: str, **options) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format=fmt, **options)
    return buf.getvalue()


def frames(width: int, height: int) -> list[Image.Image]:
    out = []
    for colour in ((200, 30, 30), (30, 200, 30), (30, 30, 200)):
        img = pattern(width, height)
        ImageDraw.Draw(img).rectangle([width // 2, height // 2, width - 1, height - 1], fill=colour)
        out.append(img)
    return out


def build_images() -> list[tuple[str, str, bytes, tuple[int, int], bool]]:
    """(filename, content type, bytes, displayed size, renderable)"""
    base = pattern(1600, 1200)
    rgba = base.convert("RGBA")
    ImageDraw.Draw(rgba).rectangle([800, 0, 1599, 599], fill=(0, 0, 0, 0))
    exif = Image.Exif()
    exif[0x0112] = 6     # Orientation: turn 90 degrees clockwise to display
    anim = frames(800, 600)
    gray16 = base.convert("L").convert("I").point(lambda v: v * 256)   # mode "I" is saved as 16-bit gray PNG
    landscape = (1600, 1200)
    return [
        ("jpeg_baseline.jpg", "image/jpeg", encode(base, "JPEG", quality=90), landscape, True),
        ("jpeg_progressive.jpg", "image/jpeg", encode(base, "JPEG", quality=90, progressive=True), landscape, True),
        ("jpeg_exif_orientation6.jpg", "image/jpeg", encode(base, "JPEG", quality=90, exif=exif.tobytes()), (1200, 1600), True),
        ("jpeg_cmyk.jpg", "image/jpeg", encode(base.convert("CMYK"), "JPEG", quality=90), landscape, True),
        ("jpeg_grayscale.jpg", "image/jpeg", encode(base.convert("L"), "JPEG", quality=90), landscape, True),
        ("jpeg_tiny_10x10.jpg", "image/jpeg", encode(pattern(10, 10), "JPEG"), (10, 10), True),
        ("jpeg_portrait_900x1600.jpg", "image/jpeg", encode(pattern(900, 1600), "JPEG"), (900, 1600), True),
        ("jpeg_64mp_8000x8000.jpg", "image/jpeg", encode(pattern(8000, 8000), "JPEG", quality=60), (8000, 8000), True),
        ("jpeg_200mp_16320x12240.jpg", "image/jpeg", encode(pattern(16320, 12240), "JPEG", quality=60), (16320, 12240), True),
        # above the 250 MP limit (storage.imgproxy.max_src_resolution_mp): a small file, many pixels
        ("png_255mp_flat.png", "image/png", encode(Image.new("1", (17000, 15000), 1), "PNG"), (17000, 15000), False),
        ("png_rgb.png", "image/png", encode(base, "PNG"), landscape, True),
        ("png_rgba_alpha.png", "image/png", encode(rgba, "PNG"), landscape, True),
        ("png_palette.png", "image/png", encode(base.convert("P"), "PNG"), landscape, True),
        ("png_16bit_gray.png", "image/png", encode(gray16, "PNG"), landscape, True),
        ("webp_lossy.webp", "image/webp", encode(base, "WEBP", quality=80), landscape, True),
        ("webp_lossless_alpha.webp", "image/webp", encode(rgba, "WEBP", lossless=True), landscape, True),
        ("webp_animated.webp", "image/webp",
         encode(anim[0], "WEBP", save_all=True, append_images=anim[1:], duration=300, loop=0), (800, 600), True),
        ("gif_static.gif", "image/gif", encode(base.convert("P"), "GIF"), landscape, True),
        ("gif_animated.gif", "image/gif",
         encode(anim[0].convert("P"), "GIF", save_all=True, append_images=[f.convert("P") for f in anim[1:]],
                duration=300, loop=0), (800, 600), True),
    ]


def run_image(headers: dict, observation_id: int, name: str, ctype: str, data: bytes,
              display: tuple[int, int], renderable: bool) -> None:
    up = requests.post(f"{API}/media/upload", headers=headers, timeout=120,
                       data={"observation_id": observation_id, "media_type": "image"},
                       files={"file": (name, data, ctype)})
    if not check(name, up.status_code == 200, "upload", f"{up.status_code} {'' if up.ok else up.text[:160]}"):
        return
    media_id = up.json()["media_file_id"]
    info = requests.get(f"{API}/media/{media_id}/info", headers=headers, timeout=10).json()
    check(name, (info.get("width"), info.get("height")) == display, "width and height as displayed",
          f"{info.get('width')}x{info.get('height')}")
    check(name, info.get("storyboard_status") == "unsupported", "no storyboard for an image", str(info.get("storyboard_status")))
    accept = {"Accept": "image/webp,image/*", **headers}

    if not renderable:
        check(name, info.get("thumbnail_status") == "unsupported" and info.get("thumbnail_urls") is None,
              "above the render limit: unsupported, no urls", f"{info.get('thumbnail_status')}")
        r = requests.get(f"{API}/media/{media_id}/thumbnail", params={"size": "small"}, headers=accept, timeout=60)
        check(name, r.status_code == 422 and r.headers.get("content-type", "").startswith("application/problem+json")
              and r.json().get("code") == "image_too_large_for_thumbnails", "thumbnail answers the problem",
              f"{r.status_code} {r.text[:120]}")
        original = requests.get(f"{API}/media/{media_id}/download", headers=headers, timeout=120)
        check(name, original.status_code == 200 and original.content == data, "the original is stored and downloads",
              f"{original.status_code} {len(original.content)} bytes")
        return

    check(name, info.get("thumbnail_status") == "ready", "thumbnail_status ready", str(info.get("thumbnail_status")))
    for size, box in BOXES.items():
        r = requests.get(f"{API}/media/{media_id}/thumbnail", params={"size": size}, headers=accept, timeout=60)
        content_type = r.headers.get("content-type", "")
        if not check(name, r.status_code == 200 and content_type.startswith("image/"), f"thumbnail {size}",
                     f"{r.status_code} {content_type} {'' if r.ok else r.text[:120]}"):
            continue
        try:
            with Image.open(io.BytesIO(r.content)) as thumb:
                got = thumb.size
        except OSError as exc:
            check(name, False, f"thumbnail {size} decodes", repr(exc)[:120])
            continue
        check(name, close_to(got, fit(display, box), 1), f"thumbnail {size} size and orientation",
              f"got {got} want {fit(display, box)} ({content_type})")
    jpeg = requests.get(f"{API}/media/{media_id}/thumbnail", params={"size": "medium", "format": "jpeg"}, headers=headers, timeout=60)
    check(name, jpeg.status_code == 200 and jpeg.headers.get("content-type") == "image/jpeg", "medium as jpeg",
          f"{jpeg.status_code} {jpeg.headers.get('content-type')}")
    public = (info.get("thumbnail_urls") or {}).get("small")
    if check(name, bool(public), "signed public url in info"):
        anon = requests.get(f"{NGINX}{public}", timeout=60)
        check(name, anon.status_code == 200, "public url without token", f"{anon.status_code}")


# --------------------------------------------------------------------------- audio

AUDIO = [   # (file name, ffmpeg codec arguments, the format's canonical content type)
    ("audio.mp3", ["-c:a", "libmp3lame", "-b:a", "96k"], "audio/mpeg"),
    ("audio.wav", ["-c:a", "pcm_s16le"], "audio/wav"),
    ("audio.flac", ["-c:a", "flac"], "audio/flac"),
    ("audio_vorbis.ogg", ["-c:a", "libvorbis"], "audio/ogg"),
    ("audio.opus", ["-c:a", "libopus"], "audio/ogg"),
    ("audio_aac.m4a", ["-c:a", "aac"], "audio/mp4"),
]
VIDEO_TYPES = {".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime",
               ".mkv": "video/matroska"}     # the canonical name each video is stored under


def run_audio(ffmpeg: str, tmp: Path, headers: dict, observation_id: int) -> None:
    for name, codec, canonical in AUDIO:
        path = tmp / name
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        "sine=frequency=440:duration=3", *codec, str(path)], check=True, timeout=120)
        up = requests.post(f"{API}/media/upload", headers=headers, timeout=120,
                           data={"observation_id": observation_id, "media_type": "audio"},
                           files={"file": (name, path.read_bytes(), "application/octet-stream")})
        if not check(name, up.status_code == 200, "upload", f"{up.status_code} {'' if up.ok else up.text[:160]}"):
            continue
        info = requests.get(f"{API}/media/{up.json()['media_file_id']}/info", headers=headers, timeout=10).json()
        check(name, info.get("mime_type") == canonical, f"stored as {canonical}", str(info.get("mime_type")))
        if info.get("playback_type"):
            check(name, info["playback_type"].split(";")[0] == canonical, "playback_type on the canonical name",
                  info["playback_type"])


def run_refusals(headers: dict, observation_id: int) -> None:
    """The sniffer decides, never the extension: bytes it cannot name and text named as a photo are 415"""
    for name, data in (("random_bytes.jpg", bytes(range(256)) * 16), ("text.png", b"not a picture\n" * 64),
                       ("html_page.webp", b"<!DOCTYPE html><html><body><script>alert(1)</script></body></html>\n")):
        r = requests.post(f"{API}/media/upload", headers=headers, timeout=60,
                          data={"observation_id": observation_id, "media_type": "image"},
                          files={"file": (name, data, "application/octet-stream")})
        check(name, r.status_code == 415 and problem_code(r) == "media_content_type_not_accepted",
              "refused: media_content_type_not_accepted", f"{r.status_code} {problem_code(r)} {r.text[:120] if r.status_code != 415 else ''}")


# --------------------------------------------------------------------------- videos

def ffmpeg_binary() -> str:
    from app.backend.services.media.posters import resolve_binary
    ffmpeg = resolve_binary("ffmpeg", imageio_fallback=True)
    if not ffmpeg:
        raise SystemExit("ffmpeg not available on the host (pip install imageio-ffmpeg)")
    return ffmpeg


def source(seconds: float, size: str, rate: int = 10, audio: bool = True) -> list[str]:
    args = ["-f", "lavfi", "-i", f"testsrc2=duration={seconds}:size={size}:rate={rate}"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    return args


H264 = ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast"]
HEVC = ["-c:v", "libx265", "-pix_fmt", "yuv420p", "-preset", "ultrafast", "-tag:v", "hvc1", "-x265-params", "log-level=error"]
VP9 = ["-c:v", "libvpx-vp9", "-b:v", "600k", "-deadline", "realtime", "-cpu-used", "8"]
ODD = ["-vf", "scale=641:361"]     # testsrc2 rounds its size to even numbers


def build_videos(ffmpeg: str, tmp: Path) -> list[dict]:
    """dict: name, path, display (w, h) or None, duration, expect ('ok' | 'short' | 'no_video' | 'broken')"""
    def run(*args: str) -> None:
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *args], check=True, timeout=600)

    hevc_10bit = [arg if arg != "yuv420p" else "yuv420p10le" for arg in HEVC]
    specs = [
        ("mp4_h264_aac.mp4", source(12, "640x360") + H264 + ["-c:a", "aac"], (640, 360), 12, "ok"),
        ("m4v_h264_aac.m4v", source(12, "640x360") + H264 + ["-c:a", "aac", "-f", "mp4"], (640, 360), 12, "ok"),
        ("mp4_h265_aac.mp4", source(12, "640x360") + HEVC + ["-c:a", "aac"], (640, 360), 12, "ok"),
        ("mp4_h265_10bit.mp4", source(12, "640x360") + hevc_10bit + ["-c:a", "aac"], (640, 360), 12, "ok"),
        ("mp4_mpeg4_part2.mp4", source(12, "640x360") + ["-c:v", "mpeg4", "-q:v", "5", "-c:a", "aac"], (640, 360), 12, "ok"),
        ("mp4_no_audio.mp4", source(12, "640x360", audio=False) + H264, (640, 360), 12, "ok"),
        ("webm_vp8_vorbis.webm", source(12, "640x360") + ["-c:v", "libvpx", "-b:v", "600k", "-c:a", "libvorbis"], (640, 360), 12, "ok"),
        ("webm_vp9_opus.webm", source(12, "640x360") + VP9 + ["-c:a", "libopus"], (640, 360), 12, "ok"),
        ("webm_av1_opus.webm", source(12, "640x360") + ["-c:v", "libaom-av1", "-cpu-used", "8", "-row-mt", "1", "-crf", "40",
                                                          "-b:v", "0", "-c:a", "libopus"], (640, 360), 12, "ok"),
        ("mov_h264_aac.mov", source(12, "640x360") + H264 + ["-c:a", "aac"], (640, 360), 12, "ok"),
        ("mov_prores_pcm.mov", source(12, "640x360") + ["-c:v", "prores_ks", "-profile:v", "0", "-c:a", "pcm_s16le"], (640, 360), 12, "ok"),
        ("mkv_h264_aac.mkv", source(12, "640x360") + H264 + ["-c:a", "aac"], (640, 360), 12, "ok"),
        ("mkv_hevc_opus.mkv", source(12, "640x360") + HEVC + ["-c:a", "libopus"], (640, 360), 12, "ok"),
        ("mkv_vp9_641x361.mkv", source(12, "640x360") + ODD + VP9 + ["-c:a", "libopus"], (641, 361), 12, "ok"),
        ("mp4_h264_yuv444_641x361.mp4", source(12, "640x360") + ODD + ["-c:v", "libx264", "-pix_fmt", "yuv444p", "-preset",
                                                                       "veryfast", "-c:a", "aac"], (641, 361), 12, "ok"),
        ("mp4_h264_4k.mp4", source(6, "3840x2160", rate=25) + H264 + ["-c:a", "aac"], (3840, 2160), 6, "ok"),
        ("mp4_h264_one_keyframe_30s.mp4", source(30, "640x360") + H264 + ["-g", "1000", "-keyint_min", "1000",
                                                                           "-sc_threshold", "0", "-c:a", "aac"], (640, 360), 30, "ok"),
        ("mp4_h264_230s.mp4", source(230, "320x180", rate=5) + H264 + ["-c:a", "aac"], (320, 180), 230, "ok"),
        ("mp4_h264_0_5s.mp4", source(0.5, "640x360") + H264 + ["-c:a", "aac"], (640, 360), 0.5, "short"),
    ]
    videos = []
    for name, args, display, duration, expect in specs:
        path = tmp / name
        run(*args, "-t", str(duration), str(path))
        videos.append({"name": name, "path": path, "display": display, "duration": duration, "expect": expect})

    plain = tmp / "mp4_h264_aac.mp4"
    rotated = tmp / "mp4_h264_rotated90.mp4"
    run("-display_rotation", "90", "-i", str(plain), "-c", "copy", str(rotated))
    videos.append({"name": rotated.name, "path": rotated, "display": (360, 640), "duration": 12, "expect": "ok"})

    audio_only = tmp / "mp4_audio_only.mp4"
    run("-f", "lavfi", "-i", "sine=frequency=440:duration=12", "-c:a", "aac", "-f", "mp4", str(audio_only))
    videos.append({"name": audio_only.name, "path": audio_only, "display": None, "duration": 12, "expect": "no_video"})

    truncated = tmp / "mp4_truncated.mp4"
    data = plain.read_bytes()
    truncated.write_bytes(data[: int(len(data) * 0.6)])     # the moov box sits at the end: unreadable
    videos.append({"name": truncated.name, "path": truncated, "display": None, "duration": None, "expect": "broken"})
    return videos


CUE = re.compile(r"(\d+):(\d+):(\d+)\.(\d+) --> (\d+):(\d+):(\d+)\.(\d+)\s*\n(\S+)#xywh=(\d+),(\d+),(\d+),(\d+)")


def seconds_of(hours: str, minutes: str, seconds: str, millis: str) -> float:
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(millis) / 1000


def through_nginx(url: str) -> str:
    """Cue URLs carry the origin nginx passed (Host without the published port)"""
    parts = urlsplit(url)
    return f"{NGINX}{parts.path}{('?' + parts.query) if parts.query else ''}"


def problem_code(response: requests.Response) -> str:
    if not response.headers.get("content-type", "").startswith("application/problem+json"):
        return f"<{response.headers.get('content-type')}>"
    return response.json().get("code", "")


def check_poster(name: str, headers: dict, media_id: int, display: tuple[int, int]) -> None:
    poster = fit(display, POSTER_MAX_SIDE)
    accept = {"Accept": "image/webp,image/*", **headers}
    for size in ("medium", "preview"):
        r = requests.get(f"{API}/media/{media_id}/thumbnail", params={"size": size}, headers=accept, timeout=60)
        if not check(name, r.status_code == 200, f"poster thumbnail {size}", f"{r.status_code}"):
            continue
        with Image.open(io.BytesIO(r.content)) as img:
            got = img.size
        check(name, close_to(got, fit(poster, BOXES[size]), 2), f"poster {size} size and orientation",
              f"got {got} want ~{fit(poster, BOXES[size])}")


def check_storyboard(name: str, video: dict, duration: float, vtt: str) -> None:
    cues = [m.groups() for m in CUE.finditer(vtt)]
    if not check(name, bool(cues), "storyboard cues", f"{len(cues)} cues"):
        return
    starts = [seconds_of(*c[0:4]) for c in cues]
    ends = [seconds_of(*c[4:8]) for c in cues]
    step = ends[0] - starts[0]
    want = math.ceil(duration / step - 1e-9)
    check(name, len(cues) == want, "one cue per step", f"{len(cues)} cues, step {step:g}s, want {want}")
    check(name, abs(ends[-1] - duration) <= 0.6, "last cue ends at the duration", f"{ends[-1]:.3f} vs {duration}")
    tile_w, tile_h = int(cues[0][11]), int(cues[0][12])
    want_h = round(TILE_WIDTH * video["display"][1] / video["display"][0])
    check(name, tile_w == TILE_WIDTH and abs(tile_h - want_h) <= 2, "tile size follows the displayed aspect",
          f"{tile_w}x{tile_h} want ~{TILE_WIDTH}x{want_h}")
    sheets: dict[str, Image.Image] = {}
    for url in dict.fromkeys(c[8] for c in cues):
        r = requests.get(through_nginx(url), timeout=60)
        if not check(name, r.status_code == 200 and r.headers.get("content-type") == "image/jpeg",
                     "sheet via /media-images/ without a token", f"{r.status_code} {r.headers.get('content-type')}"):
            continue
        sheets[url] = Image.open(io.BytesIO(r.content)).convert("L")
        grid = (GRID[0] * tile_w, GRID[1] * tile_h)
        check(name, sheets[url].size == grid, "sheet size is the #xywh grid", f"{sheets[url].size} want {grid}")
    check(name, len(sheets) == math.ceil(len(cues) / (GRID[0] * GRID[1])), "sheet count", f"{len(sheets)} for {len(cues)} cues")
    dark = []
    for index, cue in enumerate(cues):
        sheet = sheets.get(cue[8])
        if sheet is None:
            continue
        x, y, w, h = (int(v) for v in cue[9:13])
        mean = ImageStat.Stat(sheet.crop((x, y, x + w, y + h))).mean[0]
        if mean < 10:
            dark.append(f"{index}:{mean:.1f}")
    check(name, not dark, "no cue points at a black tile", f"black {dark[:8]}" if dark else f"{len(cues)} tiles lit")


def wait_for_answers(headers: dict, videos: list[dict]) -> tuple[dict, dict]:
    """Poll thumbnail and storyboard of every video until each answers something other than 202"""
    pending = {v["name"]: v for v in videos if v.get("id") is not None}
    posters: dict[str, requests.Response] = {}
    boards: dict[str, requests.Response] = {}
    started = time.time()
    while pending and time.time() < started + WAIT_SECONDS:
        for name, video in list(pending.items()):
            if name not in posters:
                r = requests.get(f"{API}/media/{video['id']}/thumbnail", params={"size": "small"}, headers=headers, timeout=60)
                if r.status_code != 202:
                    posters[name] = r
            if name in posters and name not in boards:
                r = requests.get(f"{API}/media/{video['id']}/storyboard", headers=headers, timeout=60)
                if r.status_code != 202:
                    boards[name] = r
                    note(name, f"answers after {time.time() - started:.0f}s: poster {posters[name].status_code}, storyboard {r.status_code}")
            if name in boards:
                pending.pop(name)
        time.sleep(2)
    for name in pending:
        check(name, False, f"answers within {WAIT_SECONDS}s",
              f"poster={getattr(posters.get(name), 'status_code', None)} storyboard={getattr(boards.get(name), 'status_code', None)}")
    return posters, boards


def run_videos(headers: dict, observation_id: int, videos: list[dict]) -> None:
    for video in videos:
        r = requests.post(f"{API}/media/nginx-upload", params={"observation_id": observation_id, "media_type": "video"},
                          headers={"X-Original-Filename": video["name"], "Content-Type": "application/octet-stream", **headers},
                          data=video["path"].read_bytes(), timeout=300)
        video["id"] = r.json()["media_file_id"] if check(video["name"], r.status_code == 200, "upload",
                                                          f"{r.status_code} {'' if r.ok else r.text[:200]}") else None
        if video["id"] is not None and video["expect"] == "no_video":
            check(video["name"], r.json().get("thumbnail_status") == "unsupported", "unsupported at upload",
                  str(r.json().get("thumbnail_status")))

    posters, boards = wait_for_answers(headers, videos)
    for video in videos:
        name = video["name"]
        if video.get("id") is None or name not in boards:
            continue
        poster, board = posters[name], boards[name]
        info = requests.get(f"{API}/media/{video['id']}/info", headers=headers, timeout=10).json()
        note(name, f"probe: {info.get('video_codec')}/{info.get('audio_codec')} {info.get('width')}x{info.get('height')} "
                   f"{info.get('duration_seconds')}s {info.get('playback_type')!r}")
        canonical = VIDEO_TYPES[Path(name).suffix]
        check(name, info.get("mime_type") == canonical, f"stored as {canonical}", str(info.get("mime_type")))
        if info.get("playback_type"):
            check(name, info["playback_type"].split(";")[0] == canonical, "playback_type on the canonical name",
                  info["playback_type"])
        if video["expect"] == "no_video":
            check(name, poster.status_code == 422 and problem_code(poster) == "no_video_stream", "thumbnail: no_video_stream",
                  f"{poster.status_code} {problem_code(poster)}")
            check(name, board.status_code == 422 and problem_code(board) == "no_video_stream", "storyboard: no_video_stream",
                  f"{board.status_code} {problem_code(board)}")
            check(name, (info.get("thumbnail_status"), info.get("storyboard_status")) == ("unsupported", "unsupported"),
                  "statuses unsupported", f"{info.get('thumbnail_status')} / {info.get('storyboard_status')}")
            continue
        if video["expect"] == "broken":
            check(name, poster.status_code == 409 and problem_code(poster) == "poster_failed", "thumbnail: poster_failed",
                  f"{poster.status_code} {problem_code(poster)}")
            check(name, board.status_code == 409 and problem_code(board) == "storyboard_failed", "storyboard: storyboard_failed",
                  f"{board.status_code} {problem_code(board)}")
            continue
        check(name, (info.get("width"), info.get("height")) == video["display"], "width and height as displayed",
              f"{info.get('width')}x{info.get('height')}")
        if check(name, poster.status_code == 200, "poster extracted", f"{poster.status_code} {problem_code(poster) if not poster.ok else ''}"):
            check_poster(name, headers, video["id"], video["display"])
        if video["expect"] == "short":
            check(name, board.status_code == 422 and problem_code(board) == "storyboard_unsupported",
                  "storyboard: storyboard_unsupported", f"{board.status_code} {problem_code(board)}")
            continue
        if check(name, board.status_code == 200 and board.text.startswith("WEBVTT"), "storyboard WebVTT",
                 f"{board.status_code} {problem_code(board) if not board.ok else ''}"):
            check_storyboard(name, video, info.get("duration_seconds") or video["duration"], board.text)
        check(name, (info.get("thumbnail_status"), info.get("storyboard_status")) == ("ready", "ready"),
              "statuses ready", f"{info.get('thumbnail_status')} / {info.get('storyboard_status')}")


def stream_url(media_id: int) -> str:
    return f"{API}{MediaRoutes.BASE.value}{MediaRoutes.MEDIA_STREAM.value.format(media_id=media_id)}"


def media_url(route: MediaRoutes, media_id: int) -> str:
    return f"{API}{MediaRoutes.BASE.value}{route.value.format(media_id=media_id)}"


def file_headers(response: requests.Response) -> tuple:
    return tuple(response.headers.get(name) for name in ("Content-Type", "Content-Disposition", "ETag", "X-File-Hash"))


def check_stream(headers: dict, video: dict) -> None:
    """/stream through the auth cache: the bytes, the API's headers on a miss and on a hit, HEAD, a bad range"""
    media_id, source = video["id"], video["path"].read_bytes()
    info = requests.get(media_url(MediaRoutes.MEDIA_INFO, media_id), headers=headers, timeout=10).json()
    full = requests.get(stream_url(media_id), headers=headers, timeout=60)
    if not check("stream", full.status_code == 200 and full.content == source, "full answer: the source bytes",
                 f"{full.status_code} {len(full.content)} of {len(source)}"):
        return
    content_type, disposition, etag, file_hash = file_headers(full)
    check("stream", content_type == info["mime_type"], "Content-Type is the stored type", f"{content_type} / {info['mime_type']}")
    check("stream", bool(file_hash) and etag == f'"{file_hash}"', "ETag is the API's file hash", f"{etag} / {file_hash}")
    check("stream", (disposition or "").startswith("inline"), "Content-Disposition inline", str(disposition))
    check("stream", bool(full.headers.get("X-Request-ID")), "X-Request-ID", "")
    for attempt in ("miss or hit", "hit"):
        part = requests.get(stream_url(media_id), headers={**headers, "Range": "bytes=1000-1999"}, timeout=30)
        check("stream", part.status_code == 206 and part.content == source[1000:2000]
              and part.headers.get("Content-Range") == f"bytes 1000-1999/{len(source)}",
              f"Range 1000-1999 ({attempt}): the source bytes", f"{part.status_code} {part.headers.get('Content-Range')}")
        check("stream", file_headers(part) == file_headers(full), f"Range ({attempt}): the API's headers", str(file_headers(part)))
    head = requests.head(stream_url(media_id), headers=headers, timeout=30)
    check("stream", head.status_code == 200 and head.headers.get("Content-Length") == str(len(source)), "HEAD: the size",
          f"{head.status_code} {head.headers.get('Content-Length')}")
    beyond = requests.get(stream_url(media_id), headers={**headers, "Range": f"bytes={len(source) + 10}-"}, timeout=30)
    check("stream", beyond.status_code == 416, "unsatisfiable Range: 416", str(beyond.status_code))
    digest = "sha-256=:" + base64.b64encode(hashlib.sha256(source).digest()).decode("ascii") + ":"
    check("stream", full.headers.get("Repr-Digest") == digest, "Repr-Digest is the digest of the bytes",
          str(full.headers.get("Repr-Digest")))
    for route in (MediaRoutes.MEDIA_STREAM, MediaRoutes.MEDIA_DOWNLOAD):
        url = media_url(route, media_id)
        same = requests.get(url, headers={**headers, "If-None-Match": etag}, timeout=30)
        check("stream", same.status_code == 304 and not same.content and same.headers.get("ETag") == etag,
              f"{route.name} If-None-Match: 304 with the ETag, no body", f"{same.status_code} {same.headers.get('ETag')}")
        other = requests.get(url, headers={**headers, "If-None-Match": '"other"'}, timeout=60)
        check("stream", other.status_code == 200 and other.content == source and other.headers.get("ETag") == etag,
              f"{route.name} If-None-Match of another file: the file", f"{other.status_code} {other.headers.get('ETag')}")
        again = requests.get(url, headers={**headers, "Range": "bytes=0-9", "If-Range": etag}, timeout=30)
        check("stream", again.status_code == 206 and again.headers.get("ETag") == etag
              and again.headers.get("Repr-Digest") == digest, f"{route.name} If-Range with the ETag: 206",
              f"{again.status_code} {again.headers.get('ETag')}")


# nginx keeps an allowed stream 30 s and counts whole seconds: refused at most ~31 s after the access went away
REVOKED_WITHIN = 32


def refused_within(url: str, headers: dict, status: int, seconds: float = 40) -> Optional[float]:
    """Seconds until url answers status (polled every second), None if it never did within `seconds`"""
    start = time.monotonic()
    while time.monotonic() - start <= seconds:
        if requests.get(url, headers={**headers, "Range": "bytes=0-99"}, timeout=30).status_code == status:
            return round(time.monotonic() - start, 1)
        time.sleep(1)
    return None


def check_stream_refusals(headers: dict, video: dict) -> None:
    """Refusals are the API's problems, and taking access away stops a stream within the 30 s of the cache"""
    media_id = video["id"]
    anonymous = requests.get(stream_url(media_id), timeout=30)
    check("stream", anonymous.status_code == 401 and problem_code(anonymous) == "credentials_required",
          "no token: 401 credentials_required", f"{anonymous.status_code} {problem_code(anonymous)}")
    challenges = anonymous.raw.headers.getlist("WWW-Authenticate")
    check("stream", len(challenges) == 1, "no token: one WWW-Authenticate", str(challenges))
    missing = requests.get(stream_url(10 ** 9), headers=headers, timeout=30)
    check("stream", missing.status_code == 404 and problem_code(missing) == "media_not_found", "unknown media: 404 media_not_found",
          f"{missing.status_code} {problem_code(missing)}")

    login = requests.post(f"{API}{AuthRoutes.BASE.value}{AuthRoutes.LOGIN.value}", json=LOGIN, timeout=10)
    second = {"Authorization": f"Bearer {login.json()['access_token']}"}
    allowed = requests.get(stream_url(media_id), headers={**second, "Range": "bytes=0-99"}, timeout=30)
    check("stream", allowed.status_code == 206, "second session streams", str(allowed.status_code))
    out = requests.post(f"{API}{AuthRoutes.BASE.value}{AuthRoutes.LOGOUT.value}", headers=second, timeout=10)
    check("stream", out.status_code == 200, "second session logs out", str(out.status_code))
    took = refused_within(stream_url(media_id), second, 401)
    check("stream", took is not None and took <= REVOKED_WITHIN, "logout stops the stream within ~30 s", f"after {took} s")
    note("stream", f"logout -> 401 after {took} s")

    allowed = requests.get(stream_url(media_id), headers={**headers, "Range": "bytes=0-99"}, timeout=30)
    archived = requests.post(media_url(MediaRoutes.MEDIA_ARCHIVE, media_id), headers=headers, timeout=10)
    check("stream", allowed.status_code == 206 and archived.status_code == 200, "stream, then archive",
          f"{allowed.status_code} {archived.status_code}")
    took = refused_within(stream_url(media_id), headers, 404)
    check("stream", took is not None and took <= REVOKED_WITHIN, "archive stops the stream within ~30 s", f"after {took} s")
    note("stream", f"archive -> 404 after {took} s")
    restored = requests.post(media_url(MediaRoutes.MEDIA_RESTORE, media_id), headers=headers, timeout=10)
    check("stream", restored.status_code == 200, "restore", str(restored.status_code))


def main() -> int:
    ffmpeg = ffmpeg_binary()
    token = requests.post(f"{API}/auth/login", json=LOGIN, timeout=10)
    if token.status_code != 200:
        raise SystemExit(f"login failed: {token.status_code} {token.text[:200]}")
    headers = {"Authorization": f"Bearer {token.json()['access_token']}"}
    stamp = int(time.time())
    post = requests.post(f"{API}/posts/", json={"name": f"Formats {stamp}", "code": f"FM{stamp % 100000}"}, headers=headers, timeout=10).json()
    category = requests.post(f"{API}/categories/create", json={"name": f"Formats {stamp}", "alias": f"fm{stamp}"}, headers=headers, timeout=10).json()
    signal = requests.post(f"{API}/signal-types/create", json={"code": f"FM{stamp}", "name": f"Formats {stamp}", "category_id": category["id"]},
                           headers=headers, timeout=10).json()
    observation = requests.post(f"{API}/observations/create", json={
        "post_id": post["id"], "signal_type_id": signal["id"],
        "frequencies": [{"kind": "rc", "frequency_min": "100.000", "frequency_max": "200.000"}],
        "observed_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
    }, headers=headers, timeout=10).json()

    with tempfile.TemporaryDirectory() as tmp:
        videos = build_videos(ffmpeg, Path(tmp))
        for name, content_type, data, display, renderable in build_images():
            run_image(headers, observation["id"], name, content_type, data, display, renderable)
        run_audio(ffmpeg, Path(tmp), headers, observation["id"])
        run_refusals(headers, observation["id"])
        run_videos(headers, observation["id"], videos)
        streamed = next((video for video in videos if video["expect"] == "ok" and video.get("id")), None)
        if check("stream", streamed is not None, "a stored video to stream", ""):
            check_stream(headers, streamed)
            check_stream_refusals(headers, streamed)
        matroska = next((video for video in videos if video["name"].endswith(".mkv") and video.get("id")), None)
        if check("stream", matroska is not None, "a stored MKV to stream", ""):
            check_stream(headers, matroska)
            check("stream", requests.head(stream_url(matroska["id"]), headers=headers, timeout=30)
                  .headers.get("Content-Type") == "video/matroska", "MKV served as video/matroska", "")

    print("\n==== summary")
    for name, lines in summary.items():
        bad = [line for line in lines if line.startswith("FAIL")]
        print(f"{'FAIL' if bad else 'ok  '} {name}" + (f"  <- {len(bad)} failed" if bad else ""))
    print(f"\n{'all checks passed' if not failures else f'{len(failures)} failed checks'}")
    for failure in failures:
        print("  - " + failure)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
