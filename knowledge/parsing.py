"""Worker-only format parsing in a disposable, resource-bounded subprocess."""

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from pathlib import PurePosixPath

MAX_BYTES = 10 * 1024 * 1024
MAX_CHARS = 1_000_000
MAX_OUTPUT = 8 * 1024 * 1024


class IngestionError(RuntimeError):
    """Safe machine-readable code only; input and parser errors must not escape."""


@dataclass(frozen=True)
class Section:
    heading: str
    text: str


@dataclass(frozen=True)
class ParsedDocument:
    text: str
    sections: tuple[Section, ...]
    lang: str


def _document(sections):
    sections = tuple(Section(s.heading[:1000], s.text.strip()) for s in sections if s.text.strip())
    text = "\n\n".join(s.text for s in sections)
    if not text or "\x00" in text:
        raise IngestionError("no_text")
    if len(text) > MAX_CHARS:
        raise IngestionError("extracted_too_large")
    return ParsedDocument(text, sections, "ur" if re.search("[\u0600-\u06ff]", text) else "en")


def _paragraphs(paragraphs):
    sections: list[Section] = []
    lines: list[str] = []
    heading = ""
    for is_heading, value in paragraphs:
        if is_heading:
            if lines:
                sections.append(Section(heading, "\n".join(lines)))
            heading, lines = value.strip(), []
        else:
            lines.append(value)
    if lines:
        sections.append(Section(heading, "\n".join(lines)))
    return _document(sections)


class _HTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.paragraphs = []
        self.parts = []
        self.hidden = 0
        self.heading = False

    def flush(self):
        if self.parts:
            self.paragraphs.append((self.heading, "".join(self.parts)))
            self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "template"}:
            self.hidden += 1
        if self.hidden:
            return
        if tag in {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self.flush()
            self.heading = tag.startswith("h")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "template"}:
            self.hidden = max(0, self.hidden - 1)
            return
        if not self.hidden and tag in {"p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self.flush()
            self.heading = False

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _parse(data, format):
    if format in {"txt", "md", "html"}:
        value = data.decode("utf-8-sig", errors="strict")
        if len(value) > MAX_CHARS:
            raise IngestionError("extracted_too_large")
        if format == "html":
            parser = _HTML()
            parser.feed(value)
            parser.flush()
            return _paragraphs(parser.paragraphs)
        if format == "md":
            return _paragraphs(
                (bool(re.match(r"^#{1,6}\s", line)), re.sub(r"^#{1,6}\s+", "", line))
                for line in value.splitlines()
            )
        return _document([Section("", value)])
    if format == "pdf":
        from pypdf import PdfReader

        if not data.startswith(b"%PDF-"):
            raise IngestionError("invalid_format")
        reader = PdfReader(io.BytesIO(data), strict=True)
        if reader.is_encrypted:
            raise IngestionError("encrypted_pdf")
        if len(reader.pages) > 200:
            raise IngestionError("too_many_pages")
        sections, length = [], 0
        for page in reader.pages:
            value = page.extract_text()
            # Reject mixed scanned/text documents too: never silently omit a page.
            if not value.strip():
                raise IngestionError("pdf_no_text")
            length += len(value)
            if length > MAX_CHARS:
                raise IngestionError("extracted_too_large")
            sections.append(Section("", value))
        return _document(sections)
    if format == "docx":
        from defusedxml.ElementTree import fromstring  # type: ignore[import-untyped]

        if not data.startswith(b"PK\x03\x04"):
            raise IngestionError("invalid_format")
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > 1000 or sum(x.file_size for x in entries) > 50 * 1024 * 1024:
                raise IngestionError("archive_limit")
            names = set()
            for entry in entries:
                path = PurePosixPath(entry.filename)
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or "\\" in entry.filename
                    or ":" in entry.filename
                    or entry.filename in names
                    or entry.flag_bits & 1
                    or (entry.external_attr >> 16) & 0o170000 == 0o120000
                ):
                    raise IngestionError("unsafe_archive")
                names.add(entry.filename)
                if entry.filename.endswith(".rels"):
                    root = fromstring(archive.read(entry))
                    if any(e.get("TargetMode", "").lower() == "external" for e in root.iter()):
                        raise IngestionError("external_relationship")
            if not {"[Content_Types].xml", "word/document.xml"} <= names:
                raise IngestionError("invalid_format")
            ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
            headers = sorted(n for n in names if re.fullmatch(r"word/header[0-9]+\.xml", n))
            footers = sorted(n for n in names if re.fullmatch(r"word/footer[0-9]+\.xml", n))
            parts = (
                headers
                + ["word/document.xml"]
                + [n for n in ("word/footnotes.xml", "word/endnotes.xml") if n in names]
                + footers
            )
            sections = []
            for name in sorted(names):
                if name.startswith("word/") and (
                    name.endswith(".xml") or "/embeddings/" in name or name.endswith(".bin")
                ):
                    if not name.endswith(".xml"):
                        raise IngestionError("unsupported_docx_part")
                    root = fromstring(archive.read(name))
                    if name not in parts and any(
                        e.tag
                        in {ns + "t", "{http://schemas.openxmlformats.org/drawingml/2006/main}t"}
                        and (e.text or "").strip()
                        for e in root.iter()
                    ):
                        raise IngestionError("unsupported_docx_part")
            for name in parts:
                root = fromstring(archive.read(name))
                if any(
                    e.tag
                    in {
                        ns + "altChunk",
                        ns + "txbxContent",
                        ns + "drawing",
                        ns + "pict",
                        ns + "object",
                    }
                    for e in root.iter()
                ):
                    raise IngestionError("unsupported_docx_part")
                paragraphs = []
                for paragraph in root.iter(ns + "p"):
                    style = paragraph.find("./" + ns + "pPr/" + ns + "pStyle")
                    heading = style is not None and style.get(ns + "val", "").lower().startswith(
                        "heading"
                    )
                    value = "".join(
                        e.text or "" if e.tag == ns + "t" else "\t" if e.tag == ns + "tab" else "\n"
                        for e in paragraph.iter()
                        if e.tag in {ns + "t", ns + "tab", ns + "br"}
                    )
                    paragraphs.append((heading, value))
                if any(value.strip() for _, value in paragraphs):
                    sections.extend(_paragraphs(paragraphs).sections)
            return _document(sections)
    raise IngestionError("invalid_format")


def parse_bytes(data: bytes, format: str, *, timeout=20.0, rss_limit=512 * 1024 * 1024):
    """Parent monitors RSS, wall time and output; child never receives owner secrets."""
    import psutil  # type: ignore[import-untyped]

    if len(data) > MAX_BYTES:
        raise IngestionError("input_too_large")
    if format not in {"txt", "md", "html", "pdf", "docx"}:
        raise IngestionError("invalid_format")
    start = time.monotonic()
    with tempfile.TemporaryFile() as source, tempfile.TemporaryFile() as output:
        source.write(data)
        source.seek(0)
        child = subprocess.Popen(
            [sys.executable, "-m", "knowledge.parsing", "--child", format],
            stdin=source,
            stdout=output,
            stderr=subprocess.DEVNULL,
            env={"PATH": os.defpath, "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"},
        )
        try:
            process = psutil.Process(child.pid)
            while True:
                if time.monotonic() - start >= timeout:
                    raise IngestionError("parser_timeout")
                try:
                    if process.memory_info().rss > rss_limit:
                        raise IngestionError("parser_memory")
                except psutil.NoSuchProcess:
                    pass
                if os.fstat(output.fileno()).st_size > MAX_OUTPUT:
                    raise IngestionError("parser_output")
                if child.poll() is not None:
                    break
                time.sleep(0.005)
            if child.returncode:
                raise IngestionError("parser_failed")
            output.seek(0)
            result = json.loads(output.read(MAX_OUTPUT + 1))
            if "error" in result:
                raise IngestionError(result["error"])
            return _document([Section(**s) for s in result["sections"]])
        finally:
            if child.poll() is None:
                child.kill()
            child.wait()


def _child():
    import logging
    import resource

    logging.disable(logging.CRITICAL)
    resource.setrlimit(resource.RLIMIT_CPU, (20, 20))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT, MAX_OUTPUT))
    if sys.platform == "linux":
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024,) * 2)
    try:
        data = sys.stdin.buffer.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise IngestionError("input_too_large")
        result = asdict(_parse(data, sys.argv[2]))
    except IngestionError as exc:
        result = {"error": str(exc)}
    except Exception:
        result = {"error": "invalid_document"}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    _child()
