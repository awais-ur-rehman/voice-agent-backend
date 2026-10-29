"""Real format inputs; no external files, documents or model assets."""

import io
import zipfile

import pytest


def docx(text="Hours", *, member=None, relationship=None):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Clinic</w:t></w:r></w:p><w:p><w:r><w:t>'
            + text
            + "</w:t></w:r></w:p></w:body></w:document>",
        )
        if member:
            archive.writestr(member, "payload")
        if relationship:
            archive.writestr("word/_rels/document.xml.rels", relationship)
    return output.getvalue()


def pdf(*, pages=1, text="Clinic hours", encrypted=False):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    for _ in range(pages):
        page = writer.add_blank_page(612, 792)
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
        )
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 50 700 Td ({text}) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(stream)
    if encrypted:
        writer.encrypt("synthetic-test")
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def test_utf8_markdown_headings_and_urdu():
    from knowledge.parsing import parse_bytes

    result = parse_bytes("# اوقات\nکلینک کھلا ہے۔\n\n# رابطہ\nفون کریں۔".encode(), "md")
    assert "کلینک کھلا ہے۔" in result.text
    assert result.lang == "ur"
    assert [(s.heading, s.text) for s in result.sections] == [
        ("اوقات", "کلینک کھلا ہے۔"),
        ("رابطہ", "فون کریں۔"),
    ]


def test_docx_headings_and_real_text_pdf():
    from knowledge.parsing import parse_bytes

    assert parse_bytes(docx(), "docx").sections[0].heading == "Clinic"
    assert "Clinic hours" in parse_bytes(pdf(), "pdf").text


@pytest.mark.parametrize(
    "data,format",
    [(b"\xff", "txt"), (b"not pdf", "pdf"), (b"not zip", "docx"), (b"", "txt"), (b"a\x00b", "txt")],
)
def test_bad_formats_fail_whole_document(data, format):
    from knowledge.parsing import IngestionError, parse_bytes

    with pytest.raises(IngestionError):
        parse_bytes(data, format)


@pytest.mark.parametrize(
    "kind", ["encrypted", "scanned", "pages", "traversal", "external", "archive", "entries", "text"]
)
def test_format_limits_reject_instead_of_partial_ready(kind):
    from knowledge.parsing import IngestionError, parse_bytes

    if kind == "encrypted":
        data, format = pdf(encrypted=True), "pdf"
    elif kind == "scanned":
        data, format = pdf(text=""), "pdf"
    elif kind == "pages":
        data, format = pdf(pages=201), "pdf"
    elif kind == "traversal":
        data, format = docx(member="../outside"), "docx"
    elif kind == "external":
        data, format = (
            docx(
                relationship='<Relationships><Relationship TargetMode="External" Target="https://example.com/a"/></Relationships>'
            ),
            "docx",
        )
    elif kind == "archive":
        data, format = docx(text="a" * (50 * 1024 * 1024 + 1)), "docx"
    elif kind == "entries":
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as z:
            for i in range(1001):
                z.writestr(str(i), "x")
        data, format = output.getvalue(), "docx"
    else:
        data, format = b"a" * 1_000_001, "txt"
    with pytest.raises(IngestionError):
        parse_bytes(data, format)


def test_parser_subprocess_time_and_rss_are_enforced():
    from knowledge.parsing import IngestionError, parse_bytes

    with pytest.raises(IngestionError, match="parser_timeout"):
        parse_bytes(b"valid", "txt", timeout=0.001)
    with pytest.raises(IngestionError, match="parser_memory"):
        parse_bytes(b"valid", "txt", rss_limit=1)


def test_html_never_fetches_assets_or_indexes_scripts():
    from knowledge.parsing import parse_bytes

    doc = parse_bytes(
        b'<h1>Hours</h1><p>Open today</p><script>secret()</script><img src="https://example.com/pixel">',
        "html",
    )
    assert doc.text == "Open today"
    assert doc.sections[0].heading == "Hours"


def test_docx_xml_entity_is_rejected():
    from knowledge.parsing import IngestionError, parse_bytes

    payload = docx("&xxe;").replace(b"never", b"never")
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(payload)) as source, zipfile.ZipFile(output, "w") as target:
        for entry in source.infolist():
            data = source.read(entry)
            if entry.filename == "word/document.xml":
                data = b'<!DOCTYPE x [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>' + data
            target.writestr(entry.filename, data)
    with pytest.raises(IngestionError):
        parse_bytes(output.getvalue(), "docx")


def test_docx_tables_header_footer_and_notes_in_defined_order():
    from knowledge.parsing import parse_bytes

    base = docx("Body")
    output = io.BytesIO()
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    with zipfile.ZipFile(io.BytesIO(base)) as source, zipfile.ZipFile(output, "w") as target:
        for entry in source.infolist():
            data = source.read(entry)
            if entry.filename == "word/document.xml":
                data = data.replace(
                    b"</w:body>",
                    b"<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Table cell</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body>",
                )
            target.writestr(entry.filename, data)
        for name, value in [
            ("header1", "Header"),
            ("footer1", "Footer"),
            ("footnotes", "Footnote"),
            ("endnotes", "Endnote"),
        ]:
            target.writestr(
                "word/" + name + ".xml",
                f'<w:root xmlns:w="{ns}"><w:p><w:r><w:t>{value}</w:t></w:r></w:p></w:root>',
            )
    result = parse_bytes(output.getvalue(), "docx")
    assert result.text.splitlines() == [
        "Header",
        "",
        "Body",
        "Table cell",
        "",
        "Footnote",
        "",
        "Endnote",
        "",
        "Footer",
    ]


def test_docx_unsupported_text_parts_fail_instead_of_silent_omission():
    from knowledge.parsing import IngestionError, parse_bytes

    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(docx())) as source, zipfile.ZipFile(output, "w") as target:
        for entry in source.infolist():
            target.writestr(entry.filename, source.read(entry))
        target.writestr(
            "word/glossary/document.xml",
            '<w:root xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:t>Unsupported important text</w:t></w:root>',
        )
    with pytest.raises(IngestionError, match="unsupported_docx_part"):
        parse_bytes(output.getvalue(), "docx")
