"""The one Content-Type parser and formatter (RFC 9110 section 8.3; pure, no FastAPI).

    media-type = type "/" subtype parameters
    parameters = *( OWS ";" OWS [ parameter ] )
    parameter  = parameter-name "=" parameter-value
    parameter-value = ( token / quoted-string )

type, subtype and parameter names are case-insensitive (lower case here); parameter values are kept as sent (the
quoted-pairs of a quoted-string undone). Anything else (whitespace around "/" or "=", a list of types, a duplicate
parameter, an unterminated quote, non-ASCII outside a quoted string) is malformed: parse_content_type answers None,
as for an absent value. A caller that refuses an unknown type refuses a malformed one with the same code.

The names themselves are enums/media_types.py.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Optional

from app.backend.enums.media_types import ContentType

_TOKEN = r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+"
_QUOTED = r'"(?:[\t \x21\x23-\x5b\x5d-\x7e\x80-\xff]|\\[\t \x21-\x7e\x80-\xff])*"'
_MEDIA_TYPE = re.compile(rf"({_TOKEN})/({_TOKEN})")
_PARAMETER = re.compile(rf"[ \t]*;[ \t]*(?:({_TOKEN})=({_TOKEN}|{_QUOTED}))?")
_TOKEN_ONLY = re.compile(_TOKEN)
_QUOTED_PAIR = re.compile(r"\\(.)", re.DOTALL)
_OWS = " \t"

# Text types served with a charset: WebVTT defines none (always UTF-8); RFC 7763 requires one for Markdown and spells
# it UTF-8 in its examples; every other text type the API serves is UTF-8 (the uploads are checked as such)
_NO_CHARSET = frozenset({ContentType.WEBVTT.value})
_CHARSET_SPELLING = {ContentType.TEXT_MARKDOWN.value: "UTF-8"}


@dataclass(frozen=True)
class MediaTypeValue:
    """A parsed Content-Type: type and subtype in lower case, parameters by lower-case name"""
    type: str
    subtype: str
    params: Mapping[str, str]

    @property
    def essence(self) -> str:
        """type/subtype without parameters"""
        return f"{self.type}/{self.subtype}"


def parse_content_type(value: Optional[str]) -> Optional[MediaTypeValue]:
    """The media type of a Content-Type value; None when it is absent, empty or malformed"""
    if value is None:
        return None
    text = value.strip(_OWS)
    match = _MEDIA_TYPE.match(text)
    if match is None:
        return None
    params = {}
    position = match.end()
    while position < len(text):
        parameter = _PARAMETER.match(text, position)
        if parameter is None or parameter.end() == position:
            return None
        name, raw = parameter.group(1), parameter.group(2)
        if name is not None:
            name = name.lower()
            if name in params:
                return None
            params[name] = _QUOTED_PAIR.sub(r"\1", raw[1:-1]) if raw.startswith('"') else raw
        position = parameter.end()
    return MediaTypeValue(match.group(1).lower(), match.group(2).lower(), MappingProxyType(params))


def format_content_type(essence: str, **params: str) -> str:
    """'video/mp4' and codecs='avc1.64001F, mp4a.40.2' -> 'video/mp4; codecs="avc1.64001F, mp4a.40.2"': the essence in
    lower case, each parameter as a token when it is one, else as a quoted string. ValueError when essence is not
    type/subtype"""
    parsed = parse_content_type(essence)
    if parsed is None or parsed.params:
        raise ValueError("essence must be type/subtype without parameters")
    parts = [parsed.essence]
    for name, raw in params.items():
        text = str(raw)
        if not _TOKEN_ONLY.fullmatch(text):
            text = '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
        parts.append(f"{name.lower()}={text}")
    return "; ".join(parts)


def served_content_type(essence: str) -> str:
    """The Content-Type a stored file is served with: text types carry charset=utf-8 (text/markdown charset=UTF-8,
    RFC 7763), text/vtt nothing (WebVTT is UTF-8 by definition), every other type its plain name"""
    parsed = parse_content_type(essence)
    if parsed is None:
        return essence
    name = parsed.essence
    if parsed.type != "text" or name in _NO_CHARSET:
        return name
    return format_content_type(name, charset=_CHARSET_SPELLING.get(name, "utf-8"))
