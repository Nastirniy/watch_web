"""Writes the binary fixtures of this folder (the text ones are kept as written): small, real, reproducible.

    venv/Scripts/python tests/fixtures/files/make_fixtures.py

The office files are minimal packages in the entry order Microsoft Office writes ([Content_Types].xml, _rels/.rels,
then the part folder; ODF: the stored mimetype first), which is what libmagic reads. guide.doc is a compound file
(CFB v3) with one WordDocument stream. Zip entries carry a fixed date so the bytes never change.
"""
from __future__ import annotations

import struct
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATE = (2026, 10, 1, 12, 0, 0)

OOXML = "http://schemas.openxmlformats.org/"
PKG_RELS = OOXML + "package/2006/relationships"
DOC_RELS = OOXML + "officeDocument/2006/relationships"


def write_zip(name: str, entries: list[tuple[str, str]], stored_first: bool = False) -> None:
    with zipfile.ZipFile(HERE / name, "w") as package:
        for index, (entry, text) in enumerate(entries):
            info = zipfile.ZipInfo(entry, DATE)
            info.compress_type = zipfile.ZIP_STORED if stored_first and index == 0 else zipfile.ZIP_DEFLATED
            package.writestr(info, text)


def content_types(overrides: dict[str, str]) -> str:
    parts = "".join(f'<Override PartName="/{part}" ContentType="{kind}"/>' for part, kind in overrides.items())
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            f'<Default Extension="xml" ContentType="application/xml"/>{parts}</Types>')


def rels(*targets: tuple[str, str]) -> str:
    items = "".join(f'<Relationship Id="rId{n}" Type="{DOC_RELS}/{kind}" Target="{target}"/>'
                    for n, (kind, target) in enumerate(targets, 1))
    return f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="{PKG_RELS}">{items}</Relationships>'


def docx() -> None:
    main = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
    body = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="{OOXML}wordprocessingml/2006/main">'
            '<w:body><w:p><w:r><w:t>User guide: how to record an observation.</w:t></w:r></w:p></w:body></w:document>')
    write_zip("guide.docx", [
        ("[Content_Types].xml", content_types({"word/document.xml": main})),
        ("_rels/.rels", rels(("officeDocument", "word/document.xml"))),
        ("word/document.xml", body),
    ])


def xlsx() -> None:
    sheet_ns = f'xmlns="{OOXML}spreadsheetml/2006/main"'
    write_zip("guide.xlsx", [
        ("[Content_Types].xml", content_types({
            "xl/workbook.xml": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
            "xl/worksheets/sheet1.xml": "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml",
        })),
        ("_rels/.rels", rels(("officeDocument", "xl/workbook.xml"))),
        ("xl/workbook.xml", f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook {sheet_ns} '
                            f'xmlns:r="{DOC_RELS}"><sheets><sheet name="Guide" sheetId="1" r:id="rId1"/></sheets></workbook>'),
        ("xl/_rels/workbook.xml.rels", rels(("worksheet", "worksheets/sheet1.xml"))),
        ("xl/worksheets/sheet1.xml", f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet {sheet_ns}>'
                                     '<sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>Step</t></is></c>'
                                     '<c r="B1"><v>1</v></c></row></sheetData></worksheet>'),
    ])


def pptx() -> None:
    p = f'xmlns:a="{OOXML}drawingml/2006/main" xmlns:r="{DOC_RELS}" xmlns:p="{OOXML}presentationml/2006/main"'
    head = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    tree = ('<p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
            '<p:grpSpPr/></p:spTree></p:cSld>')
    kind = "application/vnd.openxmlformats-officedocument.presentationml."
    write_zip("guide.pptx", [
        ("[Content_Types].xml", content_types({
            "ppt/presentation.xml": kind + "presentation.main+xml",
            "ppt/slides/slide1.xml": kind + "slide+xml",
            "ppt/slideLayouts/slideLayout1.xml": kind + "slideLayout+xml",
            "ppt/slideMasters/slideMaster1.xml": kind + "slideMaster+xml",
        })),
        ("_rels/.rels", rels(("officeDocument", "ppt/presentation.xml"))),
        ("ppt/presentation.xml", f'{head}<p:presentation {p}><p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/>'
                                 '</p:sldMasterIdLst><p:sldIdLst><p:sldId id="256" r:id="rId2"/></p:sldIdLst>'
                                 '<p:sldSz cx="9144000" cy="6858000"/><p:notesSz cx="6858000" cy="9144000"/></p:presentation>'),
        ("ppt/_rels/presentation.xml.rels", rels(("slideMaster", "slideMasters/slideMaster1.xml"), ("slide", "slides/slide1.xml"))),
        ("ppt/slides/slide1.xml", f"{head}<p:sld {p}>{tree}</p:sld>"),
        ("ppt/slides/_rels/slide1.xml.rels", rels(("slideLayout", "../slideLayouts/slideLayout1.xml"))),
        ("ppt/slideLayouts/slideLayout1.xml", f"{head}<p:sldLayout {p}>{tree}</p:sldLayout>"),
        ("ppt/slideLayouts/_rels/slideLayout1.xml.rels", rels(("slideMaster", "../slideMasters/slideMaster1.xml"))),
        ("ppt/slideMasters/slideMaster1.xml", f'{head}<p:sldMaster {p}>{tree}<p:sldLayoutIdLst>'
                                              '<p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst></p:sldMaster>'),
        ("ppt/slideMasters/_rels/slideMaster1.xml.rels", rels(("slideLayout", "../slideLayouts/slideLayout1.xml"))),
    ])


def odt() -> None:
    office = "urn:oasis:names:tc:opendocument:xmlns:office:1.0"
    write_zip("guide.odt", [
        ("mimetype", "application/vnd.oasis.opendocument.text"),
        ("META-INF/manifest.xml", '<?xml version="1.0" encoding="UTF-8"?><manifest:manifest '
                                  'xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" manifest:version="1.2">'
                                  '<manifest:file-entry manifest:full-path="/" manifest:media-type="application/vnd.oasis.opendocument.text"/>'
                                  '<manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/>'
                                  '</manifest:manifest>'),
        ("content.xml", f'<?xml version="1.0" encoding="UTF-8"?><office:document-content xmlns:office="{office}" '
                        'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" office:version="1.2">'
                        '<office:body><office:text><text:p>User guide</text:p></office:text></office:body>'
                        '</office:document-content>'),
    ], stored_first=True)


def doc() -> None:
    """CFB v3, 512-byte sectors: 0 the FAT, 1 the directory, 2-9 the WordDocument stream (4096 bytes, so no mini
    stream is needed); the stream starts with the Word FIB identifier"""
    end, free, fat_sector, no_stream = 0xFFFFFFFE, 0xFFFFFFFF, 0xFFFFFFFD, 0xFFFFFFFF
    header = bytearray(512)
    header[0:8] = bytes.fromhex("D0CF11E0A1B11AE1")
    struct.pack_into("<HHHHH", header, 24, 0x003E, 0x0003, 0xFFFE, 9, 6)
    struct.pack_into("<IIIIIIIII", header, 40, 0, 1, 1, 0, 4096, end, 0, end, 0)
    struct.pack_into("<109I", header, 76, 0, *([free] * 108))
    fat = struct.pack("<128I", fat_sector, end, *range(3, 10), end, *([free] * 118))

    def entry(name: str, kind: int, child: int, start: int, size: int) -> bytes:
        raw = bytearray(128)
        encoded = (name + "\0").encode("utf-16-le") if name else b""
        raw[0:len(encoded)] = encoded
        struct.pack_into("<HBBIII", raw, 64, len(encoded), kind, 1, no_stream, no_stream, child)
        struct.pack_into("<IQ", raw, 116, start, size)
        return bytes(raw)

    directory = (entry("Root Entry", 5, 1, end, 0) + entry("WordDocument", 2, no_stream, 2, 4096)
                 + entry("", 0, no_stream, 0, 0) * 2)
    stream = b"\xec\xa5\xc1\x00" + bytes(4092)
    (HERE / "guide.doc").write_bytes(bytes(header) + fat + directory + stream)


def pdf() -> None:
    text = b"BT /F1 18 Tf 72 720 Td (User guide) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(text), text),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    (HERE / "guide.pdf").write_bytes(bytes(out))


def refused() -> None:
    """Files the store must refuse: binary bytes behind a text head longer than the sniffed 8 KB, named .md; UTF-16
    text named .txt (libmagic calls both text/plain)"""
    head = b"# Guide\n\n" + b"A line of the guide in plain ASCII text.\n" * 220
    (HERE / "binary.md").write_bytes(head + bytes(range(256)) * 4)
    (HERE / "utf16.txt").write_bytes("Plain text in UTF-16, not UTF-8.\n".encode("utf-16"))


if __name__ == "__main__":
    for build in (pdf, docx, xlsx, pptx, odt, doc, refused):
        build()
