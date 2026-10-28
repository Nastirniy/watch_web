"""One registry for media and documents: seeded document rows, upload purposes, the one family rule, forbidden types."""
import ast
from pathlib import Path

import pytest

from app.backend.api.v1 import V1
from app.backend.enums.general import UploadPurpose
from app.backend.enums.routes import MediaFormatRoutes
from app.backend.services.files import formats
from app.backend.services.files.names import extension_of, sanitize_filename
from tests.helpers import make_admin

CREATE = V1.path(MediaFormatRoutes.CREATE)
BY_ID = V1.path(MediaFormatRoutes.FORMAT_BY_ID)
BACKEND = Path(__file__).resolve().parent.parent / "app" / "backend"

DOCUMENTS = {
    "application/pdf": ([".pdf"], []),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ([".docx"], ["application/zip"]),
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ([".xlsx"], ["application/zip"]),
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ([".pptx"], ["application/zip"]),
    "application/vnd.oasis.opendocument.text": ([".odt"], ["application/zip"]),
    "application/msword": ([".doc"], ["application/x-ole-storage", "application/CDFV2"]),
    "text/markdown": ([".md", ".markdown"], ["text/plain"]),
    "text/plain": ([".txt"], []),
    "text/csv": ([".csv"], ["text/plain", "application/csv"]),
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
    assert created.status_code in (200, 201), created.text
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
    assert created.status_code in (200, 201), created.text
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
