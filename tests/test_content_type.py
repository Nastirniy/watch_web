"""One Content-Type parser (app/backend/utils/content_type.py, RFC 9110 section 8.3) and one media-type vocabulary
(app/backend/enums/media_types.py, IANA names first, RFC 6838)."""
import pytest

from app.backend.enums.media_types import ContentType
from app.backend.services.files import formats
from app.backend.utils.content_type import (
    MediaTypeValue, format_content_type, parse_content_type, served_content_type,
)


@pytest.mark.parametrize("value, essence, params", [
    ("image/jpeg", "image/jpeg", {}),
    ("Image/JPEG", "image/jpeg", {}),
    ("  video/mp4  ", "video/mp4", {}),
    ("text/plain;charset=utf-8", "text/plain", {"charset": "utf-8"}),
    ("text/plain ; Charset=UTF-8", "text/plain", {"charset": "UTF-8"}),
    ("text/plain;\tcharset=utf-8 ;format=flowed", "text/plain", {"charset": "utf-8", "format": "flowed"}),
    ('video/mp4; codecs="avc1.64001F, mp4a.40.2"', "video/mp4", {"codecs": "avc1.64001F, mp4a.40.2"}),
    ('multipart/form-data; boundary="a \\"b\\" c"', "multipart/form-data", {"boundary": 'a "b" c'}),
    ('text/plain; charset=""', "text/plain", {"charset": ""}),
    ("application/problem+json", "application/problem+json", {}),
    ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
     "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", {}),
    ("text/plain;", "text/plain", {}),                     # an empty parameter is allowed (RFC 9110 5.6.6)
    ("text/plain; ; charset=utf-8", "text/plain", {"charset": "utf-8"}),
])
def test_parse_content_type(value, essence, params):
    parsed = parse_content_type(value)
    assert isinstance(parsed, MediaTypeValue)
    assert parsed.essence == essence
    assert dict(parsed.params) == params
    assert (parsed.type, parsed.subtype) == tuple(essence.split("/"))


@pytest.mark.parametrize("value", [
    None, "", "   ", "image", "image/", "/jpeg", "image/jpeg/x", "image /jpeg", "image/ jpeg", "im age/jpeg",
    "image/jpeg; charset", "image/jpeg; =utf-8", "image/jpeg; charset = utf-8", "image/jpeg; charset=utf 8",
    'image/jpeg; charset="utf-8', 'image/jpeg; charset="a"b', "image/jpeg, image/png", "image/jpeg; a=1; A=2",
    "text/plain; charset=\x00", "t\u00e9xt/plain",
])
def test_parse_content_type_refuses_malformed_values(value):
    assert parse_content_type(value) is None


def test_parameter_names_are_lower_case_values_kept():
    parsed = parse_content_type('Text/Markdown; CHARSET=UTF-8; Variant="GFM"')
    assert dict(parsed.params) == {"charset": "UTF-8", "variant": "GFM"}
    assert parsed.params.get("charset") == "UTF-8"
    with pytest.raises(TypeError):
        parsed.params["charset"] = "x"     # frozen: a parsed value is never changed


def test_format_content_type():
    assert format_content_type("text/plain", charset="utf-8") == "text/plain; charset=utf-8"
    assert format_content_type("Video/MP4", codecs="avc1.64001F, mp4a.40.2") == 'video/mp4; codecs="avc1.64001F, mp4a.40.2"'
    assert format_content_type("image/png") == "image/png"
    assert format_content_type("a/b", name='x"y\\z') == 'a/b; name="x\\"y\\\\z"'
    assert parse_content_type(format_content_type("a/b", name='x"y\\z')).params["name"] == 'x"y\\z'
    with pytest.raises(ValueError):
        format_content_type("not a type")


@pytest.mark.parametrize("essence, served", [
    ("text/plain", "text/plain; charset=utf-8"),
    ("text/csv", "text/csv; charset=utf-8"),
    ("text/markdown", "text/markdown; charset=UTF-8"),    # RFC 7763: charset is required, its example spells UTF-8
    ("text/vtt", "text/vtt"),                              # WebVTT is always UTF-8: the type defines no charset
    ("image/jpeg", "image/jpeg"),
    ("video/matroska", "video/matroska"),
    ("application/pdf", "application/pdf"),
    ("Text/Plain", "text/plain; charset=utf-8"),
])
def test_served_content_type(essence, served):
    assert served_content_type(essence) == served


def test_vocabulary_holds_iana_names_and_lower_case_values():
    values = [member.value for member in ContentType]
    assert len(values) == len(set(values))
    for value in values:
        assert value == value.lower(), value
        assert value == "*/*" or parse_content_type(value).essence == value, value
    assert ContentType.VIDEO_MATROSKA.value == "video/matroska"
    assert ContentType.PKCS12.value == "application/pkcs12"
    assert f"{ContentType.PROBLEM_JSON}" == str(ContentType.PROBLEM_JSON) == "application/problem+json"


def test_default_formats_use_the_iana_names_first():
    defaults = {spec.content_type: spec for spec in formats.default_formats()}
    expected = {
        "video/matroska": ("video/x-matroska",),
        "audio/flac": ("audio/x-flac",),
        "audio/mpeg": ("audio/mp3",),
        "audio/mp4": ("audio/x-m4a", "audio/aac"),
        "image/jpeg": ("image/pjpeg",),
        "video/mp4": ("video/x-m4v",),
        "audio/wav": ("audio/x-wav", "audio/wave", "audio/vnd.wave"),
    }
    for canonical, aliases in expected.items():
        assert defaults[canonical].aliases == aliases, canonical
    assert "video/x-matroska" not in defaults
    vocabulary = {member.value for member in ContentType}
    for spec in formats.default_formats():
        assert type(spec.content_type) is str and all(type(alias) is str for alias in spec.aliases)
        assert set(spec.all_mimes) <= vocabulary, spec.content_type


@pytest.mark.parametrize("value, canonical", [
    ("video/x-matroska", "video/matroska"),
    ("Video/X-Matroska; codecs=avc1", "video/matroska"),
    ("audio/x-wav", "audio/wav"),
    ("image/pjpeg", "image/jpeg"),
    ("video/matroska", "video/matroska"),
    ("text/plain", "text/plain"),        # the canonical name of a format of its own, also an alias of two others
    ("text/rtf", "application/rtf"),
    ("image/heic", "image/heic"),        # not a default format: kept as parsed
    ("not a type", None),
    (None, None),
])
def test_canonical_content_type(value, canonical):
    assert formats.canonical_content_type(value) == canonical


def test_opaque_types_are_the_vocabulary_members():
    assert formats.OPAQUE_CONTENT_TYPES == {ContentType.OCTET_STREAM, ContentType.BINARY_OCTET_STREAM}
    from app.backend.services.files import sniff
    assert sniff.UNKNOWN_CONTENT_TYPE == ContentType.OCTET_STREAM
    from app.backend.errors.problems import PROBLEM_JSON
    assert PROBLEM_JSON is ContentType.PROBLEM_JSON


@pytest.mark.asyncio
async def test_a_malformed_declared_type_keeps_its_415(db):
    """Malformed parses to None; a caller that refuses an unknown type keeps refusing a malformed one"""
    from app.backend.api.shared.helpers.upload_helper import check_declared_format
    from app.backend.errors.problems import ApiError
    registry = await formats.observation_registry(db)
    assert check_declared_format(registry, "video", "clip.mkv", "video/x-matroska").content_type == "video/matroska"
    assert check_declared_format(registry, "video", "clip.mkv", 'Video/Matroska; codecs="vp9"').content_type == "video/matroska"
    assert check_declared_format(registry, "video", "clip.mkv", "  ").content_type == "video/matroska"
    with pytest.raises(ApiError) as refused:
        check_declared_format(registry, "video", "clip.mkv", "video matroska")
    assert refused.value.status_code == 415
    from app.backend.enums.errors import ErrorCode
    assert refused.value.code == ErrorCode.MEDIA_CONTENT_TYPE_NOT_ACCEPTED
    assert "content_type" not in refused.value.params      # a malformed header is never echoed
    with pytest.raises(ApiError) as unknown:
        check_declared_format(registry, "video", "clip.mkv", "Video/Unknown; x=1")
    assert unknown.value.params["content_type"] == "video/unknown"


@pytest.mark.asyncio
async def test_patch_user_with_a_malformed_content_type_answers_415(client, db, headers):
    from tests.helpers import make_admin, make_user
    from tests.test_username_validation import USER
    admin = await make_admin(db)
    target = await make_user(db)
    response = await client.patch(USER.format(user_id=target.id), content=b"username=x",
                                  headers={**headers(admin), "Content-Type": "multipart form-data"})
    assert response.status_code == 415
    assert response.json()["code"] == "form_content_type_required"
    assert "content_type" not in response.json()                # the malformed header is never echoed
    as_json = await client.patch(USER.format(user_id=target.id), json={"username": "x"}, headers=headers(admin))
    assert as_json.status_code == 415 and as_json.json()["content_type"] == "application/json"
    ok = await client.patch(USER.format(user_id=target.id), data={"username_alias": "Renamed"},
                            headers=headers(admin))
    assert ok.status_code == 200, ok.text
