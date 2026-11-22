"""End-to-end check of the media pipeline against the compose.test.yml stack.

    docker build -t watch-web .
    docker compose -f tests/docker/compose.test.yml up -d
    venv/Scripts/python tests/docker/e2e.py
    docker compose -f tests/docker/compose.test.yml down -v

Walks the real path a client takes: login, create dictionary rows and an
observation, upload an image (multipart) and a video (nginx offload), fetch
thumbnails through nginx (cache MISS then HIT), load a public signed URL
without a token, reject a tampered one, wait for the media worker to extract
the video poster, download an original with a Range request, and use the
nginx-less API for the proxied renderer path. Exit code 0 when every check passes.
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests

# Ports match compose.test.yml (E2E_NGINX_PORT / E2E_DIRECT_PORT); high numbers avoid Windows reserved ranges
NGINX = os.environ.get("E2E_NGINX", f"http://127.0.0.1:{os.environ.get('E2E_NGINX_PORT', '18089')}")
DIRECT = os.environ.get("E2E_DIRECT", f"http://127.0.0.1:{os.environ.get('E2E_DIRECT_PORT', '18090')}")
API = f"{NGINX}/api/v1"
LOGIN = {
    "certificate_cn": "admin@radiomonitoring.local",
    "certificate_serial": "ABC123DEF456",
    "certificate_fingerprint": "12:34:56:78:90:AB:CD:EF:12:34:56:78:90:AB:CD:EF:12:34:56:78",
}

failures: list[str] = []


def check(condition: bool, label: str, detail: str = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"[{status}] {label}{(': ' + detail) if detail else ''}")
    if not condition:
        failures.append(label)


def wait_for(url: str, timeout: float = 180) -> None:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            r = requests.get(url, timeout=5)
            if r.status_code == 200:
                print(f"ready: {url}")
                return
            last = f"{r.status_code}"
        except requests.RequestException as exc:
            last = str(exc)[:80]
        time.sleep(2)
    raise SystemExit(f"{url} not ready after {timeout}s ({last})")


def make_png(width: int = 1200, height: int = 900) -> bytes:
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (width, height), (30, 90, 140))
    draw = ImageDraw.Draw(img)
    for i in range(0, width, 60):
        draw.rectangle([i, 0, i + 30, height], fill=(220, 180, 60))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def make_mp4(path: Path) -> Path:
    from app.backend.services.media.posters import resolve_binary
    ffmpeg = resolve_binary("ffmpeg", imageio_fallback=True)
    if not ffmpeg:
        raise SystemExit("ffmpeg not available on the host (pip install imageio-ffmpeg)")
    subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=4:size=640x360:rate=10",
         "-pix_fmt", "yuv420p", str(path)],
        check=True, timeout=120,
    )
    return path


def read_events(response, wanted: str, timeout: float = 15) -> tuple[list[dict], str]:
    """(messages, last position) of an open text/event-stream response until a message of type wanted"""
    import json
    messages, position, fields = [], "", {}
    deadline = time.time() + timeout
    for line in response.iter_lines(decode_unicode=True):
        if time.time() > deadline:
            break
        if line:
            name, _, value = line.partition(": ")
            fields[name] = value
            continue
        if "id" in fields:
            position = fields["id"]
        if "data" in fields:
            messages.append(json.loads(fields["data"]))
            if messages[-1]["type"] == wanted:
                break
        fields = {}
    return messages, position


def check_events(headers: dict, post_id: int) -> None:
    """A stream through nginx: ready first, a scope.changed of an archive arrives, a reconnect with Last-Event-ID
    replays the event it missed"""
    stream_headers = {**headers, "Accept": "text/event-stream"}
    stream = requests.get(f"{API}/events/stream", headers=stream_headers, stream=True, timeout=30)
    check(stream.status_code == 200 and stream.headers.get("content-type", "").startswith("text/event-stream"),
          "event stream opens through nginx", f"{stream.status_code} {stream.headers.get('content-type')}")
    first, _ = read_events(stream, "ready")
    check(bool(first) and first[0]["type"] == "ready", "event stream starts with ready")
    archived = requests.post(f"{API}/posts/{post_id}/archive", headers=headers, timeout=10)
    check(archived.status_code == 200, "archive a post for an event", archived.text[:200])
    seen, position = read_events(stream, "scope.changed")
    event = next((m for m in seen if m["type"] == "scope.changed"), None)
    check(event is not None and event.get("scope") == {"kind": "post", "id": post_id}, "scope.changed arrives",
          f"{seen[-1:] if seen else 'none'}")
    stream.close()
    restored = requests.post(f"{API}/posts/{post_id}/restore", headers=headers, timeout=10)
    check(restored.status_code == 200, "restore the post while disconnected", restored.text[:200])
    time.sleep(3)       # every worker has read it (poll 1 s)
    again = requests.get(f"{API}/events/stream", headers={**stream_headers, "Last-Event-ID": position},
                         stream=True, timeout=30)
    replayed, _ = read_events(again, "scope.changed")
    check(any(m["type"] == "scope.changed" and m.get("id") != (event or {}).get("id") for m in replayed),
          "a reconnect with Last-Event-ID replays the missed event", f"{[m['type'] for m in replayed]}")
    again.close()


def main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    wait_for(f"{NGINX}/health")
    wait_for(f"{DIRECT}/health")

    token = requests.post(f"{API}/auth/login", json=LOGIN, timeout=10)
    check(token.status_code == 200, "login through nginx", token.text[:200])
    headers = {"Authorization": f"Bearer {token.json()['access_token']}"}
    stamp = int(time.time())

    # --- fixtures: post, category, signal type, observation
    post = requests.post(f"{API}/posts/", json={"name": f"E2E post {stamp}", "code": f"E2E{stamp % 100000}"}, headers=headers, timeout=10)
    check(post.status_code == 200, "create post", post.text[:200])
    post_id = post.json()["id"]
    category = requests.post(f"{API}/categories/create", json={"name": f"E2E cat {stamp}", "alias": f"e2e{stamp}"}, headers=headers, timeout=10)
    check(category.status_code == 200, "create category", category.text[:200])
    signal = requests.post(f"{API}/signal-types/create", json={"code": f"E2E{stamp}", "name": f"E2E signal {stamp}", "category_id": category.json()["id"]}, headers=headers, timeout=10)
    check(signal.status_code == 200, "create signal type", signal.text[:200])
    observation = requests.post(f"{API}/observations/create", json={
        "post_id": post_id, "signal_type_id": signal.json()["id"],
        "frequencies": [{"kind": "rc", "frequency_min": "100.000", "frequency_max": "200.000"}],
        "observed_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
    }, headers=headers, timeout=10)
    check(observation.status_code == 200, "create observation", observation.text[:300])
    observation_id = observation.json()["id"]

    # --- image upload (multipart, spooled to disk by the API)
    png = make_png()
    upload = requests.post(
        f"{API}/media/upload", headers=headers, timeout=60,
        data={"observation_id": observation_id, "media_type": "image"},
        files={"file": ("e2e.png", png, "image/png")},
    )
    check(upload.status_code == 200, "multipart image upload", upload.text[:300])
    image_id = upload.json()["media_file_id"]
    check(upload.json()["thumbnail_status"] == "ready", "image upload reports thumbnail ready")
    check(bool(upload.json()["thumbnail_urls"]), "image upload carries signed thumbnail urls")

    # --- thumbnail through nginx: X-Accel-Redirect -> imgproxy -> cache
    accept = {"Accept": "image/webp,image/*", **headers}
    first = requests.get(f"{API}/media/{image_id}/thumbnail", params={"size": "medium"}, headers=accept, timeout=30)
    check(first.status_code == 200, "thumbnail via nginx", f"{first.status_code} {first.headers.get('content-type')} {first.headers.get('x-cache')}")
    check(first.headers.get("content-type") == "image/webp", "WebP negotiated by Accept", first.headers.get("content-type", ""))
    check(first.headers.get("x-cache") == "MISS", "first render is a cache MISS", first.headers.get("x-cache", ""))
    second = requests.get(f"{API}/media/{image_id}/thumbnail", params={"size": "medium"}, headers=accept, timeout=30)
    check(second.headers.get("x-cache") == "HIT", "second request is a cache HIT", second.headers.get("x-cache", ""))
    check(second.content == first.content and len(first.content) > 500, "cached body identical", f"{len(first.content)} bytes")
    from PIL import Image
    with Image.open(io.BytesIO(first.content)) as thumb:
        check(max(thumb.size) == 300, "medium thumbnail fits 300 px box", f"{thumb.size}")
    jpeg = requests.get(f"{API}/media/{image_id}/thumbnail", params={"size": "preview", "format": "jpeg"}, headers=headers, timeout=30)
    check(jpeg.status_code == 200 and jpeg.headers.get("content-type") == "image/jpeg", "preview as jpeg", jpeg.headers.get("content-type", ""))

    # --- public signed URL, no token
    info = requests.get(f"{API}/media/{image_id}/info", headers=headers, timeout=10).json()
    public = info["thumbnail_urls"]["small"]
    anon = requests.get(f"{NGINX}{public}", timeout=30)
    check(anon.status_code == 200 and anon.headers.get("content-type", "").startswith("image/"), "public signed url without token", f"{anon.status_code}")
    segments = public.split("/")
    sig = segments[2]
    segments[2] = ("A" if sig[0] != "A" else "B") + sig[1:]
    tampered = requests.get(f"{NGINX}{'/'.join(segments)}", timeout=30)
    check(tampered.status_code == 403, "tampered signature rejected by imgproxy", f"{tampered.status_code}")
    share = requests.post(f"{API}/media/{image_id}/share", json={"size": "large", "ttl_seconds": 600}, headers=headers, timeout=10)
    check(share.status_code == 200, "share link issued", share.text[:200])
    shared = requests.get(f"{NGINX}{share.json()['url']}", timeout=30)
    check(shared.status_code == 200, "share link loads", f"{shared.status_code}")

    # --- original download through nginx with Range
    download = requests.get(f"{API}/media/{image_id}/download", headers=headers, timeout=30)
    check(download.status_code == 200 and download.content == png, "download original via nginx", f"{download.headers.get('x-served-by')}")
    check(download.headers.get("x-served-by") == "nginx", "original served by nginx (X-Accel-Redirect)")
    ranged = requests.get(f"{API}/media/{image_id}/download", headers={"Range": "bytes=0-99", **headers}, timeout=30)
    check(ranged.status_code == 206 and len(ranged.content) == 100, "range request honoured", f"{ranged.status_code} {len(ranged.content)}")

    # --- video: nginx upload offload, then the media worker
    with tempfile.TemporaryDirectory() as tmp:
        video = make_mp4(Path(tmp) / "e2e.mp4").read_bytes()
    vupload = requests.post(
        f"{API}/media/nginx-upload", params={"observation_id": observation_id, "media_type": "video"},
        headers={"X-Original-Filename": "e2e.mp4", "Content-Type": "application/octet-stream", **headers},
        data=video, timeout=120,
    )
    check(vupload.status_code == 200, "nginx offloaded video upload", vupload.text[:300])
    video_id = vupload.json()["media_file_id"]
    check(vupload.json()["thumbnail_status"] == "pending", "video starts pending")
    vinfo = requests.get(f"{API}/media/{video_id}/info", headers=headers, timeout=10).json()
    check(vinfo.get("video_codec") == "h264" and 3.5 <= (vinfo.get("duration_seconds") or 0) <= 4.5,
          "video probed at upload", f"{vinfo.get('video_codec')} {vinfo.get('duration_seconds')}s {vinfo.get('width')}x{vinfo.get('height')}")
    check((vinfo.get("playback_type") or "").startswith('video/mp4; codecs="avc1.'), "playback_type for canPlayType", vinfo.get("playback_type", ""))
    pending = requests.get(f"{API}/media/{video_id}/thumbnail", headers=headers, timeout=30)
    check(pending.status_code in (202, 200), "video thumbnail 202 while pending", f"{pending.status_code} retry-after={pending.headers.get('retry-after')}")

    deadline = time.time() + 90
    poster = None
    while time.time() < deadline:
        poster = requests.get(f"{API}/media/{video_id}/thumbnail", headers=accept, timeout=30)
        if poster.status_code == 200:
            break
        time.sleep(2)
    check(poster is not None and poster.status_code == 200, "video poster extracted by the worker", f"{poster.status_code if poster else 'none'}")
    if poster is not None and poster.status_code == 200:
        with Image.open(io.BytesIO(poster.content)) as frame:
            check(max(frame.size) == 300, "video thumbnail fits 300 px box", f"{frame.size}")
    status = requests.get(f"{API}/media/posters/status", headers=headers, timeout=10).json()
    check(status["worker_alive"] is True, "media worker heartbeat visible", f"{status['posters']}")
    check(status["posters"]["ready"] >= 1, "poster counted as ready")

    # --- storyboard: WebVTT whose cues point at signed sheet urls served without a token
    deadline = time.time() + 120
    vtt = None
    while time.time() < deadline:
        vtt = requests.get(f"{API}/media/{video_id}/storyboard", headers=headers, timeout=30)
        if vtt.status_code == 200:
            break
        time.sleep(2)
    check(vtt is not None and vtt.status_code == 200, "video storyboard generated by the worker", f"{vtt.status_code if vtt else 'none'}")
    if vtt is not None and vtt.status_code == 200:
        check(vtt.text.startswith("WEBVTT"), "storyboard body is WebVTT", vtt.text[:20].replace("\n", " "))
        cues = [line for line in vtt.text.splitlines() if "#xywh=" in line]
        check(bool(cues), "storyboard has cues with xywh fragments", f"{len(cues)} cues")
        if cues:
            from urllib.parse import urlsplit, urlunsplit
            from app.backend.config import StoryboardSettings

            sheet_url, fragment = cues[0].split("#xywh=")
            _, _, tile_width, tile_height = (int(value) for value in fragment.split(","))
            cue, nginx = urlsplit(sheet_url), urlsplit(NGINX)
            # Absolute (a player resolves cues against a blob: URL), on the request origin as nginx passed it:
            # proxy_set_header Host $host, which has no port
            check(cue.scheme == nginx.scheme and cue.hostname == nginx.hostname and cue.path.startswith("/media-images/"),
                  "storyboard cue urls are absolute on the request origin", sheet_url[:60])
            check("/cb:" in cue.path, "storyboard sheet url carries the cache buster of its generation", cue.path[:80])
            # The test stack publishes nginx on another port than the Host the API saw: put the port back to fetch it
            sheet = requests.get(urlunsplit(cue._replace(netloc=nginx.netloc)), timeout=30)
            check(sheet.status_code == 200 and sheet.headers.get("content-type", "").startswith("image/jpeg"),
                  "storyboard sheet via nginx without token", f"{sheet.status_code} {sheet.headers.get('content-type')}")
            if sheet.status_code == 200:
                grid = StoryboardSettings()  # compose.test.yml keeps the grid at its defaults
                with Image.open(io.BytesIO(sheet.content)) as image:
                    check(image.size == (grid.columns * tile_width, grid.rows * tile_height),
                          "storyboard sheet keeps its size through imgproxy (#xywh stays valid)",
                          f"{image.size}, tiles {tile_width}x{tile_height}, grid {grid.columns}x{grid.rows}")

    # --- resumable upload with a simulated interruption
    import hashlib
    session = requests.post(f"{API}/media/uploads", headers=headers, timeout=10, json={
        "observation_id": observation_id, "media_type": "video", "original_filename": "resumable.mp4",
        "expected_size": len(video), "content_type": "video/mp4", "sha256": hashlib.sha256(video).hexdigest(),
    })
    check(session.status_code == 201, "open upload session", session.text[:200])
    upload_id = session.json()["id"]
    chunk_headers = {"Content-Type": "application/offset+octet-stream", **headers}
    half = len(video) // 2
    first = requests.patch(f"{API}/media/uploads/{upload_id}", data=video[:half], headers={"Upload-Offset": "0", **chunk_headers}, timeout=60)
    check(first.status_code == 204 and first.headers.get("upload-offset") == str(half), "first chunk stored", f"{first.status_code} offset={first.headers.get('upload-offset')}")
    # "connection dropped": ask where to continue, and prove a stale retry is refused
    head = requests.head(f"{API}/media/uploads/{upload_id}", headers=headers, timeout=10)
    check(head.headers.get("upload-offset") == str(half), "offset recovered after interruption", head.headers.get("upload-offset", ""))
    stale = requests.patch(f"{API}/media/uploads/{upload_id}", data=video[:half], headers={"Upload-Offset": "0", **chunk_headers}, timeout=60)
    check(stale.status_code == 409 and stale.headers.get("upload-offset") == str(half), "stale chunk refused with the real offset", f"{stale.status_code}")
    rest = requests.patch(f"{API}/media/uploads/{upload_id}", data=video[half:], headers={"Upload-Offset": str(half), **chunk_headers}, timeout=60)
    check(rest.status_code == 204, "remaining chunk stored", f"{rest.status_code}")
    done = requests.post(f"{API}/media/uploads/{upload_id}/complete", headers=headers, timeout=120)
    check(done.status_code == 200 and done.json().get("upload_id") == upload_id, "session completed into a media file", done.text[:200])
    if done.status_code == 200:
        resumed_id = done.json()["media_file_id"]
        rinfo = requests.get(f"{API}/media/{resumed_id}/info", headers=headers, timeout=10).json()
        check(rinfo.get("file_size") == len(video) and rinfo.get("video_codec") == "h264", "resumed file stored and probed", f"{rinfo.get('file_size')} {rinfo.get('video_codec')}")
        again = requests.post(f"{API}/media/uploads/{upload_id}/complete", headers=headers, timeout=60)
        check(again.status_code == 200 and again.json().get("media_file_id") == resumed_id, "complete is idempotent")

    # --- real-time events through nginx
    check_events(headers, post_id)

    # --- nginx-less API: proxied renderer and FileResponse
    dtoken = requests.post(f"{DIRECT}/api/v1/auth/login", json=LOGIN, timeout=10)
    dheaders = {"Authorization": f"Bearer {dtoken.json()['access_token']}"}
    direct = requests.get(f"{DIRECT}/api/v1/media/{image_id}/thumbnail", params={"size": "small"}, headers=dheaders, timeout=30)
    check(direct.status_code == 200 and direct.headers.get("content-type", "").startswith("image/"), "thumbnail proxied by the API without nginx", f"{direct.status_code} {direct.headers.get('content-type')}")
    ddownload = requests.get(f"{DIRECT}/api/v1/media/{image_id}/download", headers={"Range": "bytes=0-9", **dheaders}, timeout=30)
    check(ddownload.status_code == 206 and len(ddownload.content) == 10, "FileResponse range without nginx", f"{ddownload.status_code}")

    # --- observation list carries readiness and urls
    listing = requests.get(f"{API}/observations/{observation_id}", headers=headers, timeout=10)
    if listing.status_code == 200 and "media_files" in listing.json():
        media = {m["id"]: m for m in listing.json()["media_files"]}
        check(media[image_id]["thumbnail_status"] == "ready" and media[video_id]["thumbnail_status"] == "ready", "observation media carry thumbnail_status")

    print()
    if failures:
        print(f"{len(failures)} check(s) failed: {failures}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
