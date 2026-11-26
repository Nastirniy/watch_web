"""One content sniffer for every upload (services/files/sniff.py): libmagic on the first 8 KB against the format's
content type and aliases, and the text rule for text/* formats (UTF-8, a BOM allowed, no NUL byte).

The fixtures in tests/fixtures/files are small real files (make_fixtures.py writes the binary ones). The registry
follows Debian's libmagic (the api image, production): re-check the aliases there whenever the image or the defaults
change, from the repository root (<repo path>: the absolute path of the checkout, with forward slashes on Windows):

    docker build -t watch-web:e2e .
    docker run --rm -v "<repo path>:/repo:ro" -w /repo -e MYSETTINGS_APP__ENVIRONMENT=development watch-web:e2e python -m pytest tests/media/test_file_sniff.py -q -p no:cacheprovider

Expected: every test passes, none skipped. A libmagic of another system (Windows, macOS) may name a file otherwise:
the image decides.
"""
from pathlib import Path

import pytest

from tests.paths import APP_DIR, FIXTURE_FILES
from app.backend.api.shared.helpers.upload_helper import validation_config_for
from app.backend.enums.errors import ErrorCode
from app.backend.enums.general import UploadPurpose
from app.backend.services.files import sniff
from app.backend.services.files.formats import FormatRegistry, UnsupportedMediaFormat, default_formats
from app.backend.services.media_manager import MediaManager

FIXTURES = FIXTURE_FILES
GUIDE = FormatRegistry(list(default_formats())).restricted(UploadPurpose.USER_GUIDE)
DOCUMENTS = ["guide.pdf", "guide.docx", "guide.xlsx", "guide.pptx", "guide.odt", "guide.doc", "guide.md",
             "guide.txt", "guide.csv", "guide.rtf"]


def spec_of(name: str):
    spec = GUIDE.resolve(UploadPurpose.USER_GUIDE, name)
    assert spec is not None, name
    return spec


@pytest.mark.parametrize("name", DOCUMENTS)
def test_detect_reports_the_seeded_type_or_alias(name):
    detected = sniff.detect_content_type(FIXTURES / name)
    assert detected == detected.lower() and ";" not in detected
    assert detected in {mime.lower() for mime in spec_of(name).all_mimes}, detected


def test_detect_reads_only_the_head_and_cuts_parameters(tmp_path, monkeypatch):
    seen = []

    def from_buffer(buffer, mime=False):
        seen.append((len(buffer), mime))
        return "Text/Plain; charset=utf-8"

    monkeypatch.setattr(sniff.magic, "from_buffer", from_buffer)
    path = tmp_path / "big.txt"
    path.write_bytes(b"a" * (sniff.SNIFF_BYTES * 3))
    assert sniff.detect_content_type(path) == "text/plain"
    assert seen == [(sniff.SNIFF_BYTES, True)]


def test_detect_without_an_answer_is_octet_stream(tmp_path, monkeypatch):
    monkeypatch.setattr(sniff.magic, "from_buffer", lambda buffer, mime=False: "")
    path = tmp_path / "x.bin"
    path.write_bytes(b"\x01\x02")
    assert sniff.detect_content_type(path) == "application/octet-stream"


@pytest.mark.parametrize("name", ["guide.md", "guide.txt", "guide.csv"])
def test_utf8_text_passes(name):
    sniff.check_text(FIXTURES / name, spec_of(name).content_type)


CYRILLIC = "\u0406\u043d\u0441\u0442\u0440\u0443\u043a\u0446\u0456\u044f"   # Instruction, in Ukrainian


@pytest.mark.parametrize("data", [
    "\ufeffBOM first, then text".encode("utf-8"),
    f"Cyrillic {CYRILLIC} split over chunks".encode("utf-8"),
    b"",
], ids=["bom", "split", "empty"])
def test_text_rule_accepts(tmp_path, data):
    path = tmp_path / "x.txt"
    path.write_bytes(data)
    sniff.check_text(path, "text/plain", chunk_size=3)     # chunks cut multi-byte characters


@pytest.mark.parametrize("data", [
    CYRILLIC.encode("cp1251"),
    b"text with a NUL\x00 byte",
    "ends inside a character \u0406".encode("utf-8")[:-1],
    "UTF-16".encode("utf-16"),
], ids=["cp1251", "nul", "truncated", "utf16"])
def test_text_rule_refuses(tmp_path, data):
    path = tmp_path / "x.txt"
    path.write_bytes(data)
    with pytest.raises(UnsupportedMediaFormat) as refused:
        sniff.check_text(path, "text/plain", chunk_size=4)
    assert refused.value.code == ErrorCode.DOCUMENT_TEXT_INVALID
    assert refused.value.params == {"media_type": "document", "content_type": "text/plain"}


async def store(db, tmp_path: Path, fixture: str) -> UnsupportedMediaFormat:
    """The store path refuses the fixture (named as the fixture says) before any row: the error it raises"""
    manager = MediaManager(storage_path=str(tmp_path / "media"), temp_path=str(tmp_path / "temp"))
    source = tmp_path / "temp" / "upload.part"
    source.write_bytes((FIXTURES / fixture).read_bytes())
    spec = spec_of(fixture)
    with pytest.raises(UnsupportedMediaFormat) as refused:
        await manager.store_file_from_path(db, 1, str(source), fixture, spec.media_type,
                                           validation_config=validation_config_for(spec))
    assert refused.value.code.status == 415
    return refused.value


@pytest.mark.asyncio
async def test_html_named_pdf_is_415(db, tmp_path):
    refused = await store(db, tmp_path, "html_page.pdf")
    assert refused.code == ErrorCode.MEDIA_CONTENT_TYPE_NOT_ACCEPTED
    assert refused.params["content_type"] == "text/html"


@pytest.mark.asyncio
async def test_binary_markdown_is_415_document_text_invalid(db, tmp_path):
    assert sniff.detect_content_type(FIXTURES / "binary.md") == "text/plain", "the head of the file is text"
    refused = await store(db, tmp_path, "binary.md")
    assert refused.code == ErrorCode.DOCUMENT_TEXT_INVALID
    assert refused.params == {"media_type": "document", "content_type": "text/markdown"}


@pytest.mark.asyncio
async def test_utf16_text_is_415(db, tmp_path):
    refused = await store(db, tmp_path, "utf16.txt")
    assert refused.code == ErrorCode.DOCUMENT_TEXT_INVALID
    assert refused.params == {"media_type": "document", "content_type": "text/plain"}


def test_document_text_invalid_never_carries_the_bytes():
    spec = ErrorCode.DOCUMENT_TEXT_INVALID
    assert spec.status == 415
    assert set(spec.params) == {"media_type", "accepted_content_types", "accepted_extensions", "content_type"}


def test_the_sniffer_is_the_only_user_of_libmagic():
    app = APP_DIR
    users = sorted(str(path.relative_to(app)).replace("\\", "/") for path in app.rglob("*.py")
                   if "import magic" in path.read_text(encoding="utf-8"))
    assert users == ["backend/services/files/sniff.py"]
