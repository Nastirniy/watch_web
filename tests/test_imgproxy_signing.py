"""imgproxy path building and signing, and the URL helpers built on them."""
import base64
import hashlib
import hmac
from datetime import datetime, timezone

import pytest

from app.backend.services.media.imgproxy import (
    ImgproxySigner, build_processing_path, encode_source, signed_path, UNSAFE_SIGNATURE,
)
from app.backend.services.media.urls import quantised_expiry

KEY = "943b421c9eb07c830af81030552c86009268de4e532ba2ee2eab8247c6da0881"
SALT = "520f986b998545b4785e0defbc4f3c1203f22de2374a3d53cb7a7fe9fea309c5"


def test_encode_source_is_base64url_of_local_url():
    encoded = encode_source("2026/09/02/obs_017/91_abc.jpg")
    padded = encoded + "=" * (-len(encoded) % 4)
    assert base64.urlsafe_b64decode(padded) == b"local:///2026/09/02/obs_017/91_abc.jpg"
    assert "=" not in encoded
    assert encode_source("\\win\\style\\path.jpg") == encode_source("win/style/path.jpg")


def test_encode_source_rejects_traversal_and_empty():
    with pytest.raises(ValueError):
        encode_source("../etc/passwd")
    with pytest.raises(ValueError):
        encode_source("")


def test_processing_path_layout():
    path = build_processing_path("a/b.jpg", 300, 300, fmt="webp", quality=85)
    assert path == f"/rs:fit:300:300:0:0/q:85/{encode_source('a/b.jpg')}.webp"
    auto = build_processing_path("a/b.jpg", 150, 150, fmt="auto")
    assert auto.endswith(encode_source("a/b.jpg")), "auto format has no extension so imgproxy negotiates by Accept"
    expiring = build_processing_path("a/b.jpg", 300, 300, expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc))
    assert "/exp:1893456000/" in expiring


def test_signature_matches_reference_hmac():
    signer = ImgproxySigner.from_hex(KEY, SALT)
    path = "/rs:fit:300:300:0:0/" + encode_source("x.jpg")
    expected = hmac.new(bytes.fromhex(KEY), bytes.fromhex(SALT) + path.encode(), hashlib.sha256).digest()
    expected_b64 = base64.urlsafe_b64encode(expected).decode().rstrip("=")
    assert signer.sign(path) == expected_b64
    assert signed_path(signer, path) == f"/{expected_b64}{path}"
    assert signer.sign(path + "x") != expected_b64, "any change to the path changes the signature"


def test_unsigned_mode_when_no_key():
    signer = ImgproxySigner.from_hex("", "")
    assert not signer.enabled
    assert signed_path(signer, "/rs:fit:1:1:0:0/abc").startswith(f"/{UNSAFE_SIGNATURE}/")
    with pytest.raises(ValueError):
        ImgproxySigner.from_hex("not-hex", SALT)


def test_quantised_expiry_is_stable_inside_a_window():
    now = datetime(2026, 9, 4, 12, 17, 33, tzinfo=timezone.utc)
    later = datetime(2026, 9, 4, 12, 40, 0, tzinfo=timezone.utc)
    first = quantised_expiry(3600, now)
    assert first == quantised_expiry(3600, later), "same window, same expiry, same cache key"
    assert 3600 <= (first - now).total_seconds() <= 7200
    assert first.minute == 0 and first.second == 0
    next_window = quantised_expiry(3600, datetime(2026, 9, 4, 13, 0, 1, tzinfo=timezone.utc))
    assert next_window > first
