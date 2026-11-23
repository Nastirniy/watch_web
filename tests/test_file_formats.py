"""One registry for media and documents: seeded document rows, upload purposes, the one family rule, forbidden types."""
import ast
from pathlib import Path

import pytest

from app.backend.api.v1 import V1
from app.backend.enums.general import UploadPurpose
from sqlmodel import select

from app.backend.enums.routes import MediaFormatRoutes
from app.backend.models.media_format import MediaFormat
from app.backend.services.files import formats
from app.backend.services.files.names import extension_of, sanitize_filename
from tests.helpers import make_admin

CREATE = V1.path(MediaFormatRoutes.CREATE)
BY_ID = V1.path(MediaFormatRoutes.FORMAT_BY_ID)
ARCHIVE = V1.path(MediaFormatRoutes.FORMAT_ARCHIVE)
RESTORE = V1.path(MediaFormatRoutes.FORMAT_RESTORE)
BACKEND = Path(__file__).resolve().parent.parent / "app" / "backend"

# Aliases are what Debian's libmagic (the api image, 5.46) reports for real samples (tests/test_file_sniff.py):
# OOXML, ODF and Word 97 files by their own types, Markdown as text/plain, short CSV files as text/plain, RTF as text/rtf
DOCUMENTS = {
    "application/pdf": ([".pdf"], []),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ([".docx"], []),
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ([".xlsx"], []),
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ([".pptx"], []),
    "application/vnd.oasis.opendocument.text": ([".odt"], []),
    "application/msword": ([".doc"], []),
    "text/markdown": ([".md", ".markdown"], ["text/plain"]),
    "text/plain": ([".txt"], []),
    "text/csv": ([".csv"], ["text/plain"]),
    "application/rtf": ([".rtf"], ["text/rtf"]),
}


@pytest.mark.asyncio
async def test_document_formats_are_seeded(db):
    registry = await formats.load_registry(db, use_cache=False)
    documents = {spec.content_type: spec for spec in registry.formats_for("document")}
    assert set(documents) == set(DOCUMENTS)
    for content_type, (extensions, aliases) in DOCUMENTS.items():
        spec = documents[content_type]
        assert list(spec.extensions) == extensions and list(spec.aliases) == aliases, content_type
        assert spec.max_file_size_bytes == 100 * formats.MB


@pytest.mark.asyncio
async def test_observation_purpose_refuses_a_pdf(db):
    registry = await formats.load_registry(db, use_cache=False)
    assert registry.resolve(UploadPurpose.OBSERVATION_MEDIA, "plan.pdf") is None
    assert registry.resolve(UploadPurpose.OBSERVATION_MEDIA, "a.jpg").content_type == "image/jpeg"
    assert "application/pdf" not in registry.accept_post_header(UploadPurpose.OBSERVATION_MEDIA)


@pytest.mark.asyncio
async def test_guide_purpose_refuses_a_jpeg(db):
    registry = await formats.load_registry(db, use_cache=False)
    assert registry.resolve(UploadPurpose.USER_GUIDE, "a.jpg") is None
    assert registry.resolve(UploadPurpose.USER_GUIDE, "Guide.PDF").content_type == "application/pdf"
    assert registry.accept_post_header(UploadPurpose.USER_GUIDE).startswith("application/pdf")


@pytest.mark.asyncio
async def test_media_format_api_accepts_document_with_application_type(client, db, headers):
    admin = await make_admin(db)
    body = {"media_type": "document", "content_type": "application/epub+zip", "label": "EPUB",
            "extensions": [".epub"], "max_file_size_bytes": 10 * formats.MB}
    created = await client.post(CREATE, json=body, headers=headers(admin))
    assert created.status_code == 200, created.text
    assert created.json()["media_type"] == "document"
    # the family rule still holds for media: an image cannot be application/*
    wrong = dict(body, media_type="image", content_type="application/x-wrong", extensions=[".wrg"])
    assert (await client.post(CREATE, json=wrong, headers=headers(admin))).status_code == 422


@pytest.mark.asyncio
async def test_forbidden_content_type_is_422(client, db, headers):
    admin = await make_admin(db)
    body = {"media_type": "document", "content_type": "text/html", "label": "HTML",
            "extensions": [".html"], "max_file_size_bytes": formats.MB}
    refused = await client.post(CREATE, json=body, headers=headers(admin))
    assert refused.status_code == 422
    assert refused.json()["errors"][0]["code"] == "content_type_forbidden"
    alias = dict(body, content_type="text/x-ok", aliases=["application/javascript"], extensions=[".okx"])
    assert (await client.post(CREATE, json=alias, headers=headers(admin))).status_code == 422
    ok = dict(body, content_type="text/x-ok", extensions=[".okx"])
    created = await client.post(CREATE, json=ok, headers=headers(admin))
    assert created.status_code == 200, created.text
    format_id = created.json()["id"]
    for change in ({"content_type": "image/svg+xml", "media_type": "image"}, {"aliases": ["text/html"]}):
        assert (await client.put(BY_ID.format(format_id=format_id), json=change, headers=headers(admin))).status_code == 422


def test_family_rule():
    assert formats.content_type_allowed("document", "application/pdf")
    assert formats.content_type_allowed("document", "text/csv")
    assert not formats.content_type_allowed("document", "image/png")
    assert formats.content_type_allowed("image", "image/png")
    assert not formats.content_type_allowed("image", "application/pdf")
    assert not formats.content_type_allowed("unknown", "image/png")


def test_family_rule_has_one_implementation():
    for relative in ("api/v1/media_formats.py", "api/v1/models/request/media_formats.py"):
        source = (BACKEND / relative).read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "startswith":
                assert not any(isinstance(arg, ast.JoinedStr) for arg in node.args), relative
        assert 'startswith(f"{' not in source, relative


def test_names():
    assert extension_of("Plan.PDF") == ".pdf"
    assert extension_of("noext") == "" and extension_of(None) == "" and extension_of("a.") == ""
    assert sanitize_filename("photo.png") == "photo.png"


def test_default_formats_have_unique_content_types_and_extensions():
    defaults = formats.default_formats()
    content_types = [spec.content_type for spec in defaults]
    extensions = [ext for spec in defaults for ext in spec.extensions]
    assert len(content_types) == len(set(content_types))
    assert len(extensions) == len(set(extensions))


@pytest.mark.asyncio
@pytest.mark.parametrize("content_type", [
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "text/markdown", "text/csv"])
async def test_seeded_document_row_can_be_updated_archived_and_restored(client, db, headers, content_type):
    admin = await make_admin(db)
    row = (await db.exec(select(MediaFormat).where(MediaFormat.content_type == content_type))).one()
    url = BY_ID.format(format_id=row.id)
    updated = await client.put(url, json={"label": "Renamed"}, headers=headers(admin))
    assert updated.status_code == 200, updated.text
    assert (await client.post(ARCHIVE.format(format_id=row.id), headers=headers(admin))).status_code == 200
    restored = await client.post(RESTORE.format(format_id=row.id), headers=headers(admin))
    assert restored.status_code == 200, restored.text


@pytest.mark.asyncio
async def test_second_format_claiming_an_extension_is_still_a_conflict(client, db, headers):
    admin = await make_admin(db)
    body = {"media_type": "document", "content_type": "application/x-second-docx", "label": "Second",
            "extensions": [".docx"], "aliases": ["application/zip"], "max_file_size_bytes": formats.MB}
    clash = await client.post(CREATE, json=body, headers=headers(admin))
    assert clash.status_code == 400 and clash.json()["code"] == "media_format_extension_clash"


@pytest.mark.parametrize("name", [".jpg", "..jpg", ".JPG", "photo.tar.jpg", "\u0444\u043e\u0442\u043e.jpg", ".hidden.png"])
def test_sanitize_filename_reads_the_extension_with_the_one_parser(name):
    """The declared check (extension_of) and the store (sanitize_filename, then extension_of) agree: a leading-dot
    name kept its extension in the first and lost it in the second (os.path.splitext)"""
    assert extension_of(sanitize_filename(name)) == extension_of(name) != ""


@pytest.mark.asyncio
async def test_a_leading_dot_name_is_stored_like_it_is_declared(db, tmp_path):
    from app.backend.api.shared.helpers.upload_helper import check_declared_format, validation_config_for
    from app.backend.services.media_manager import MediaManager
    from tests.helpers import make_dictionary, make_observation, make_post, make_user
    from tests.test_media_formats import PNG_BYTES

    registry = await formats.observation_registry(db)
    spec = check_declared_format(registry, "image", ".png")
    post = await make_post(db)
    ids = await make_dictionary(db)
    user = await make_user(db, post_id=post.id)
    observation = await make_observation(db, user, post, ids["signal_type_id"])
    manager = MediaManager(storage_path=str(tmp_path / "media"), temp_path=str(tmp_path / "temp"))
    source = tmp_path / "temp" / "upload.part"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(PNG_BYTES)
    media_id = await manager.store_file_from_path(db, observation.id, str(source), ".png", "image", user_id=user.id,
                                                  validation_config=validation_config_for(spec))
    assert media_id


@pytest.mark.asyncio
@pytest.mark.parametrize("opaque", ["application/octet-stream", "binary/octet-stream", "Application/Octet-Stream"])
async def test_an_opaque_type_is_never_a_format_type_or_alias(client, db, headers, opaque):
    """application/octet-stream is what the sniffer answers for bytes it cannot name: as an alias it would switch the
    content check off for the format, as a content type it names no format"""
    admin = await make_admin(db)
    body = {"media_type": "document", "content_type": opaque, "label": "Opaque", "extensions": [".opq"],
            "max_file_size_bytes": formats.MB}
    for refused_body in (body, dict(body, content_type="application/x-opq", aliases=[opaque])):
        refused = await client.post(CREATE, json=refused_body, headers=headers(admin))
        assert refused.status_code == 422, refused.text
        assert refused.json()["errors"][0]["code"] == "content_type_opaque"
        assert refused.json()["errors"][0]["content_type"] == opaque.lower()
    created = await client.post(CREATE, json=dict(body, content_type="application/x-opq"), headers=headers(admin))
    assert created.status_code == 200, created.text
    url = BY_ID.format(format_id=created.json()["id"])
    for change in ({"content_type": opaque}, {"aliases": [opaque]}):
        refused = await client.put(url, json=change, headers=headers(admin))
        assert refused.status_code == 422 and refused.json()["errors"][0]["code"] == "content_type_opaque"


@pytest.mark.asyncio
async def test_an_opaque_alias_written_before_the_rule_never_accepts(db):
    """A row written before the API refused opaque aliases (or by hand) keeps them in the table, but the registry
    never accepts them: application/octet-stream is the sniffer's answer for unknown bytes"""
    db.add(MediaFormat(media_type="document", content_type="application/x-old", label="Old", extensions=[".old"],
                       aliases=["application/octet-stream", "Binary/Octet-Stream", "application/x-old2"],
                       max_file_size_bytes=formats.MB))
    await db.commit()
    registry = await formats.load_registry(db, use_cache=False)
    spec = next(spec for spec in registry.formats_for("document") if spec.content_type == "application/x-old")
    assert spec.all_mimes == ("application/x-old", "application/x-old2")
    assert "application/octet-stream" not in registry.mime_set("document")
    from app.backend.api.shared.helpers.upload_helper import validation_config_for
    assert validation_config_for(spec).allowed_mime_types == {"application/x-old", "application/x-old2"}


SVG = b'<?xml version="1.0"?>\n<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>\n'


@pytest.mark.asyncio
async def test_a_forbidden_type_written_before_the_rule_never_accepts(client, db, headers):
    """An image/svg+xml format created before content_type_forbidden existed (or by hand), and an alias that runs
    code on another row: the registry leaves both out of what the formats accept, as it does opaque types, and logs
    each row once; an .svg upload is refused 415"""
    from loguru import logger

    from app.backend.enums.routes import MediaRoutes
    from tests.test_media_formats import _scene

    db.add(MediaFormat(media_type="image", content_type="image/svg+xml", label="SVG", extensions=[".svg"],
                       aliases=["Image/SVG+XML; charset=utf-8"], max_file_size_bytes=formats.MB))
    db.add(MediaFormat(media_type="document", content_type="application/x-old", label="Old", extensions=[".old"],
                       aliases=["text/html", "application/x-old2"], max_file_size_bytes=formats.MB))
    await db.commit()
    lines = []
    sink = logger.add(lambda message: lines.append(message.record["message"]), level="WARNING")
    try:
        for _ in range(2):
            registry = await formats.load_registry(db, use_cache=False)
    finally:
        logger.remove(sink)
    # a format left with nothing it accepts is no accepted format: not in the registry at all
    assert all(".svg" not in spec.extensions for spec in registry.specs)
    svg_row = (await db.exec(select(MediaFormat).where(MediaFormat.content_type == "image/svg+xml"))).one()
    assert formats.FormatSpec.from_row(svg_row).all_mimes == ()
    old = next(spec for spec in registry.formats_for("document") if spec.content_type == "application/x-old")
    assert old.all_mimes == ("application/x-old", "application/x-old2")
    assert "image/svg+xml" not in registry.mime_set() and "text/html" not in registry.mime_set()
    refused = [line for line in lines if "types no format may accept" in line]
    assert len(refused) == 2, refused        # one per row, not one per load
    assert any("image/svg+xml" in line for line in refused) and any("text/html" in line for line in refused)

    formats.invalidate_registry_cache()
    user, observation = await _scene(db)
    data = {"observation_id": observation.id, "media_type": "image"}
    for declared in ("image/svg+xml", "application/octet-stream"):
        response = await client.post(V1.path(MediaRoutes.UPLOAD), data=data, headers=headers(user),
                                     files={"file": ("drawing.svg", SVG, declared)})
        assert response.status_code == 415, response.text
        assert response.json()["code"] == "media_extension_not_accepted"
        assert "image/svg+xml" not in response.headers["Accept-Post"]


@pytest.mark.asyncio
async def test_a_format_that_accepts_nothing_is_left_out_of_the_public_list(client, db, headers):
    """A legacy image/svg+xml row (forbidden content type, forbidden alias): GET /media/formats and Accept-Post leave
    it out, the administration list shows the row as it is"""
    from app.backend.enums.routes import MediaRoutes
    from tests.test_media_formats import _scene

    db.add(MediaFormat(media_type="image", content_type="image/svg+xml", label="SVG", extensions=[".svg"],
                       aliases=["text/html"], max_file_size_bytes=formats.MB))
    await db.commit()
    formats.invalidate_registry_cache()
    user, _ = await _scene(db)
    listed = await client.get(V1.path(MediaRoutes.FORMATS), headers=headers(user))
    assert listed.status_code == 200, listed.text
    assert all(item["content_type"] != "image/svg+xml" for item in listed.json()["formats"]["image"])
    assert "image/svg+xml" not in listed.headers["Accept-Post"]
    assert "image/png" in listed.headers["Accept-Post"]

    admin = await make_admin(db)
    rows = await client.get(V1.path(MediaFormatRoutes.LIST_ALL), headers=headers(admin))
    assert rows.status_code == 200, rows.text
    svg = next(row for row in rows.json() if row["content_type"] == "image/svg+xml")
    assert (svg["extensions"], svg["aliases"], svg["is_active"]) == ([".svg"], ["text/html"], True)
