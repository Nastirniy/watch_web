"""imgproxy URL building and signing.

imgproxy URL layout (https://docs.imgproxy.net/usage/processing):

    /{signature}/{option}/{option}/.../{base64url(source)}[.{extension}]

The signature is base64url(HMAC-SHA256(key, salt + path)) without padding,
where path is everything after the signature segment, including its leading
slash. With no key and salt configured the signature segment is "unsafe",
which imgproxy only accepts when it runs without IMGPROXY_KEY / IMGPROXY_SALT.

Sources are local files under the uploads root (IMGPROXY_LOCAL_FILESYSTEM_ROOT):
    local:///2026/09/02/obs_017/91_abc.jpg
The relative path always comes from the database, never from the client.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from app.backend.enums.general import ThumbnailFormat

UNSAFE_SIGNATURE = "unsafe"

_EXTENSIONS = {
    ThumbnailFormat.JPEG.value: "jpg",
    ThumbnailFormat.WEBP.value: "webp",
}


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def encode_source(relative_path: str) -> str:
    """base64url of the local:// source URL for a path relative to the uploads root"""
    clean = relative_path.replace("\\", "/").lstrip("/")
    if not clean or ".." in clean.split("/"):
        raise ValueError(f"refusing to encode source path {relative_path!r}")
    return _b64url(f"local:///{clean}".encode("utf-8"))


@dataclass(frozen=True)
class ImgproxySigner:
    """HMAC signer; key and salt are the hex strings from settings.storage.imgproxy"""
    key: bytes = b""
    salt: bytes = b""

    @classmethod
    def from_hex(cls, key_hex: str, salt_hex: str) -> "ImgproxySigner":
        if not key_hex or not salt_hex:
            return cls()
        try:
            return cls(key=binascii.unhexlify(key_hex), salt=binascii.unhexlify(salt_hex))
        except (binascii.Error, ValueError) as exc:
            raise ValueError("imgproxy key and salt must be hex strings") from exc

    @property
    def enabled(self) -> bool:
        return bool(self.key and self.salt)

    def sign(self, path: str) -> str:
        """Signature segment for a path that starts with '/' (options and source)"""
        if not self.enabled:
            return UNSAFE_SIGNATURE
        digest = hmac.new(self.key, self.salt + path.encode("utf-8"), hashlib.sha256).digest()
        return _b64url(digest)


def build_processing_path(
    source_relative_path: str,
    width: int,
    height: int,
    fmt: str = ThumbnailFormat.AUTO.value,
    quality: Optional[int] = None,
    expires_at: Optional[datetime] = None,
) -> str:
    """Options plus encoded source, starting with '/', without the signature.

    rs:fit keeps the aspect ratio inside the box and never enlarges.
    exp: makes imgproxy reject the URL after the timestamp (used for public links).
    """
    options = [f"rs:fit:{int(width)}:{int(height)}:0:0"]
    if quality:
        options.append(f"q:{int(quality)}")
    if expires_at is not None:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        options.append(f"exp:{int(expires_at.timestamp())}")
    path = "/" + "/".join(options) + "/" + encode_source(source_relative_path)
    ext = _EXTENSIONS.get(fmt)
    if ext:
        path += f".{ext}"
    return path


def signed_path(signer: ImgproxySigner, processing_path: str) -> str:
    """Full imgproxy request path: /{signature}{processing_path}"""
    return f"/{signer.sign(processing_path)}{processing_path}"
