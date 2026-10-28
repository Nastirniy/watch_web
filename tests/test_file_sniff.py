"""One content sniffer for every upload (services/files/sniff.py): libmagic on the first 8 KB against the format's
content type and aliases.

The fixtures in tests/fixtures/files are small real files (make_fixtures.py writes the binary ones). The registry
follows Debian's libmagic (the api image, production): re-check the aliases there whenever the image or the defaults
change, from the repository root (on Windows the mount is the full path, e.g. E:/Develop/pypr/watch_web:/repo:ro):

    docker build -t watch-web .
    docker run --rm -v "$PWD:/repo:ro" -w /repo -e MYSETTINGS_APP__ENVIRONMENT=development watch-web python -m pytest tests/test_file_sniff.py -q -p no:cacheprovider

Expected: every test passes, none skipped. A libmagic of another system (Windows, macOS) may name a file otherwise:
the image decides.
"""
from pathlib import Path

import pytest

from app.backend.api.shared.helpers.upload_helper import validation_config_for
from app.backend.enums.errors import ErrorCode
from app.backend.enums.general import UploadPurpose
from app.backend.services.files import sniff
from app.backend.services.files.formats import FormatRegistry, UnsupportedMediaFormat, default_formats
from app.backend.services.media_manager import MediaManager

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "files"
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


def test_the_sniffer_is_the_only_user_of_libmagic():
    app = Path(__file__).resolve().parents[1] / "app"
    users = sorted(str(path.relative_to(app)).replace("\\", "/") for path in app.rglob("*.py")
                   if "import magic" in path.read_text(encoding="utf-8"))
    assert users == ["backend/services/files/sniff.py"]
