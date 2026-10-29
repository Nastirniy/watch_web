"""One media-type vocabulary: no string constant in app/backend names a media type outside app/backend/enums/media_types.py
(migrations freeze their own copies). A media type, a media range or a "type/" prefix check written as a literal is
refused; docstrings and comments are not code and stay free. Tests may keep literals."""
import ast
import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "app" / "backend"
VOCABULARY = BACKEND / "enums" / "media_types.py"
MIGRATIONS = BACKEND / "migrations"

# IANA top-level types (RFC 6838 section 4.2, plus font RFC 8081 and haptics RFC 9695) and "binary" (not registered,
# sent by some clients for octet-stream). The media range */* is also a glob: it is refused where FastAPI and the
# OpenAPI helpers take media types (media_type= and file_response), any string constant there is.
TOP_LEVEL = ("application", "audio", "example", "font", "haptics", "image", "message", "model", "multipart", "text",
             "video", "binary")
SHAPED = re.compile(rf"^\s*(?:{'|'.join(TOP_LEVEL)})/[A-Za-z0-9!#$&^_.+*-]*\s*(?:;.*)?$", re.IGNORECASE | re.DOTALL)


def _docstrings(tree: ast.AST) -> set:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def media_type_literals(source: str, name: str = "<source>") -> list:
    tree = ast.parse(source, filename=name)
    docstrings = _docstrings(tree)
    found = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings
                and SHAPED.match(node.value)):
            found.append((node.lineno, node.value))
        if isinstance(node, ast.Call):
            taken = [keyword.value for keyword in node.keywords if keyword.arg == "media_type"]
            name = getattr(node.func, "id", getattr(node.func, "attr", ""))
            if name == "file_response" and len(node.args) > 1 and isinstance(node.args[1], (ast.List, ast.Tuple)):
                taken.extend(node.args[1].elts)
            found.extend((value.lineno, value.value) for value in taken
                         if isinstance(value, ast.Constant) and isinstance(value.value, str) and "/" in value.value
                         and not SHAPED.match(value.value))
    return sorted(found)


def test_the_scan_finds_what_it_must():
    source = '''
"""Module docstring naming image/jpeg is fine"""
A = "image/jpeg"
B = ["text/vtt; charset=utf-8"]
C = value.startswith("image/")
D = file_response("Files", ["*/*"], status=206)
E = f"application/vnd.openxmlformats-officedocument.{kind}"
F = "binary/octet-stream"
G = "/api/v1/media"            # a path
H = "an image/jpeg in prose"   # prose
I = Response(media_type="*/*")
J = root.glob("*/*")           # a glob
K = ApiError(code, media_type=media_type, other="video")
def f():
    """image/png in a docstring"""
    return "Video/MP4"
'''
    assert media_type_literals(source) == [
        (3, "image/jpeg"), (4, "text/vtt; charset=utf-8"), (5, "image/"), (6, "*/*"),
        (7, "application/vnd.openxmlformats-officedocument."), (8, "binary/octet-stream"), (11, "*/*"),
        (16, "Video/MP4")]


def test_no_media_type_literal_outside_the_vocabulary():
    offenders = []
    for path in sorted(BACKEND.rglob("*.py")):
        if path == VOCABULARY or MIGRATIONS in path.parents:
            continue
        for line, value in media_type_literals(path.read_text(encoding="utf-8"), str(path)):
            offenders.append(f"{path.relative_to(BACKEND).as_posix()}:{line}: {value!r}")
    assert offenders == [], "use app.backend.enums.media_types.ContentType:\n" + "\n".join(offenders)


def test_the_vocabulary_is_the_only_place_and_migrations_do_not_import_it():
    assert media_type_literals(VOCABULARY.read_text(encoding="utf-8"))
    for path in MIGRATIONS.rglob("*.py"):
        assert "enums.media_types" not in path.read_text(encoding="utf-8"), path


# The names of the media vocabulary used alone: a top-level type, a stream type of ffprobe, a Content-Disposition
# type, the media type of a row and the upload transport each have their enum. Their values are written only in
# app/backend/enums (the enum is the one set of those names), never as a string constant elsewhere in app/backend.
ENUMS = BACKEND / "enums"


def _vocabulary_names() -> dict:
    from app.backend.enums.general import MediaType, UploadTransport
    from app.backend.enums.media_types import Disposition, StreamType, TopLevelType

    names = {}
    for enum in (TopLevelType, StreamType, Disposition, MediaType, UploadTransport):
        for member in enum:
            names.setdefault(member.value.lower(), []).append(f"{enum.__name__}.{member.name}")
    return names


def vocabulary_name_literals(source: str, names: dict, name: str = "<source>") -> list:
    tree = ast.parse(source, filename=name)
    docstrings = _docstrings(tree)
    return sorted((node.lineno, node.value) for node in ast.walk(tree)
                  if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings
                  and node.value.strip().lower() in names)


def test_the_name_scan_finds_what_it_must():
    source = '''
"""video in a docstring is fine"""
A = kind == "video"
B = media_type in ("Image", "audio")
C = disposition_header("attachment", "x")
D = record(transport="nginx_offload")
E = "a video in prose"
F = {"message": "m", "model": "x"}
'''
    assert vocabulary_name_literals(source, _vocabulary_names()) == [
        (3, "video"), (4, "Image"), (4, "audio"), (5, "attachment"), (6, "nginx_offload")]


def test_vocabulary_names_used_alone_come_from_their_enum():
    from app.backend.services.files.formats import ALLOWED_TOP_LEVEL
    from app.backend.enums.media_types import TopLevelType

    names = _vocabulary_names()
    offenders = []
    for path in sorted(BACKEND.rglob("*.py")):
        if ENUMS in path.parents or MIGRATIONS in path.parents:
            continue
        for line, value in vocabulary_name_literals(path.read_text(encoding="utf-8"), names, str(path)):
            offenders.append(f"{path.relative_to(BACKEND).as_posix()}:{line}: {value!r} "
                             f"(use {' or '.join(names[value.strip().lower()])})")
    assert offenders == [], "a vocabulary name written as a literal:\n" + "\n".join(offenders)
    assert all(isinstance(name, TopLevelType) for names in ALLOWED_TOP_LEVEL.values() for name in names)
