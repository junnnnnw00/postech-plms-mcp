"""Bounded, in-memory document extraction. No files, macros, links or formulas run.

Locators deliberately describe the source document, rather than fabricated citations.
Image-only documents require OCR outside this module.
"""

from __future__ import annotations

import io
import posixpath
import re
import struct
import zipfile
import zlib
from html.parser import HTMLParser
from pathlib import PurePosixPath
from typing import Any
from xml.etree import ElementTree as ET

MAX_INPUT_BYTES = 25 * 1024 * 1024
MAX_EXPANDED_BYTES = 100 * 1024 * 1024
MAX_MEMBER_BYTES = 25 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 3000
MAX_TEXT_CHARS = 2_000_000
MAX_SEGMENTS = 5000
MAX_COMPRESSION_RATIO = 250
MAX_MEMBER_NAME_CHARS = 1024
TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".rst",
    ".csv",
    ".tsv",
    ".json",
    ".jsonl",
    ".xml",
    ".html",
    ".htm",
    ".log",
    ".srt",
    ".vtt",
    ".py",
    ".pyi",
    ".ipynb",
    ".c",
    ".h",
    ".cpp",
    ".cc",
    ".hpp",
    ".cxx",
    ".java",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".sql",
    ".r",
    ".jl",
    ".rs",
    ".go",
    ".m",
    ".swift",
    ".kt",
    ".sh",
    ".bash",
    ".zsh",
    ".ps1",
    ".bat",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".tex",
    ".bib",
    ".css",
    ".scss",
    ".s",
    ".asm",
    ".v",
    ".sv",
    ".cmake",
    ".make",
    ".mk",
    ".in",
    ".out",
    ".dat",
}
TEXT_FILENAMES = {"readme", "license", "makefile", "cmakelists.txt", "dockerfile"}
DOCUMENT_EXTENSIONS = {".pdf", ".docx", ".pptx", ".xlsx", ".hwpx", ".hwp"}
ARCHIVE_EXTENSIONS = {".zip", ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz", ".tgz"}


class ExtractionError(ValueError):
    """Expected malformed, unsupported, or unsafe document error."""


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _clean(text: str) -> str:
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text).strip()


def _xml(data: bytes) -> ET.Element:
    # ElementTree does not resolve external entities, and disallowing declarations
    # also prevents internal entity expansion on all supported Python versions.
    declaration_probe = data.replace(b"\x00", b"").upper()
    if b"<!DOCTYPE" in declaration_probe or b"<!ENTITY" in declaration_probe:
        raise ExtractionError("XML entity/DOCTYPE declarations are not accepted")
    return ET.fromstring(data)


class _Result:
    def __init__(self, filename: str, kind: str):
        self.filename = filename
        self.kind = kind
        self.segments: list[dict[str, Any]] = []
        self.warnings: list[str] = []
        self.characters = 0
        self.truncated = False

    def add(self, locator: str, text: str, **metadata: Any) -> None:
        text = _clean(text)
        if not text:
            return
        remaining = MAX_TEXT_CHARS - self.characters
        if remaining <= 0 or len(self.segments) >= MAX_SEGMENTS:
            self.truncated = True
            return
        if len(text) > remaining:
            text = text[:remaining]
            self.truncated = True
        self.characters += len(text)
        self.segments.append({"locator": locator, "text": text, **metadata})

    def finish(self, error: str | None = None) -> dict[str, Any]:
        if self.truncated:
            self.warnings.append("Text extraction limit reached; document is incomplete.")
        if not self.segments and not error:
            self.warnings.append("No extractable text found. Scans/images may require OCR.")
        return {
            "status": "error" if error else ("ok" if self.segments else "no_text"),
            "filename": self.filename,
            "format": self.kind,
            "text": "\n\n".join(s["text"] for s in self.segments),
            "segments": self.segments,
            "warnings": list(dict.fromkeys(self.warnings)),
            "truncated": self.truncated,
            **({"error": error} if error else {}),
        }


class _Archive:
    def __init__(self, data: bytes):
        self.zip = zipfile.ZipFile(io.BytesIO(data))
        entries = self.zip.infolist()
        if len(entries) > MAX_ARCHIVE_MEMBERS:
            raise ExtractionError("Archive has too many members")
        self.names: set[str] = set()
        expanded = 0
        for info in entries:
            name = info.filename
            if (
                "\\" in name
                or name.startswith("/")
                or ".." in PurePosixPath(name).parts
                or re.match(r"^[A-Za-z]:", name)
                or re.search(r"[\x00-\x1f\x7f]", info.orig_filename)
            ):
                raise ExtractionError("Archive contains an unsafe member path")
            if len(name) > MAX_MEMBER_NAME_CHARS:
                raise ExtractionError("Archive member name is too long")
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise ExtractionError("Archive symbolic links are unsupported")
            if name in self.names:
                raise ExtractionError("Archive contains duplicate member names")
            self.names.add(name)
            expanded += info.file_size
            if info.flag_bits & 1:
                raise ExtractionError("Encrypted archive members are unsupported")
            if info.file_size > MAX_MEMBER_BYTES or expanded > MAX_EXPANDED_BYTES:
                raise ExtractionError("Archive expanded size limit exceeded")
            if (
                info.file_size > 1024 * 1024
                and info.file_size / max(1, info.compress_size) > MAX_COMPRESSION_RATIO
            ):
                raise ExtractionError("Archive compression ratio exceeds safe limit")

    def read(self, path: str) -> bytes:
        # Central-directory sizes are validated, and the stream read is separately
        # bounded so forged metadata cannot cause unbounded decompression.
        with self.zip.open(path) as stream:
            data = stream.read(MAX_MEMBER_BYTES + 1)
            if len(data) > MAX_MEMBER_BYTES:
                raise ExtractionError("Archive member expanded size limit exceeded")
            return data

    def xml(self, path: str) -> ET.Element:
        return _xml(self.read(path))


def _packaged_document(archive: _Archive) -> bool:
    return (
        "word/document.xml" in archive.names
        or "ppt/presentation.xml" in archive.names
        or "xl/workbook.xml" in archive.names
        or any(re.fullmatch(r"Contents/section\d+\.xml", path) for path in archive.names)
    )


def _archive_listing(archive: _Archive, filename: str = "archive.zip") -> dict[str, Any]:
    result = _Result(filename, "zip")
    entries = []
    expanded = 0
    for info in archive.zip.infolist():
        extension = PurePosixPath(info.filename.lower()).suffix
        expanded += info.file_size
        readable = (
            extension in TEXT_EXTENSIONS | DOCUMENT_EXTENSIONS
            or PurePosixPath(info.filename.lower()).name in TEXT_FILENAMES
        )
        entries.append(
            {
                "name": info.filename,
                "is_directory": info.is_dir(),
                "compressed_bytes": info.compress_size,
                "uncompressed_bytes": info.file_size,
                "extension": extension,
                "readable_document": readable and not info.is_dir(),
            }
        )
        result.add(
            f"archive-member:{info.filename}",
            f"{info.filename} ({info.file_size} bytes{' ; directory' if info.is_dir() else ''})",
        )
    result.warnings.append(
        "ZIP members are listed without executing or extracting files. Select an exact member name to read its contents; nested archives are unsupported."
    )
    output = result.finish()
    output.update(
        status="archive", entries=entries, member_count=len(entries), total_uncompressed_bytes=expanded
    )
    if not entries:
        output["warnings"] = ["This ZIP archive is empty."]
    return output


def extract_archive(data: bytes, member: str | None = None) -> dict[str, Any]:
    """List a ZIP or read one exact member, entirely in memory.

    Every member's metadata is validated before listing or opening a selected
    member. Directory entries, links, encrypted members and nested general-purpose
    archives cannot be read. DOCX/PPTX/XLSX/HWPX packaged documents remain readable.
    Selected documents preserve their own page/line locators plus archive_member.
    """
    result = _Result("archive.zip", "zip")
    if not isinstance(data, bytes):
        return result.finish("Archive data must be bytes")
    if len(data) > MAX_INPUT_BYTES:
        return result.finish(f"Archive exceeds {MAX_INPUT_BYTES // (1024 * 1024)} MiB input limit")
    if member is not None and not isinstance(member, str):
        return result.finish("Archive member must be an exact filename string")
    archive = None
    try:
        archive = _Archive(data)
        if member is None:
            return _archive_listing(archive)
        if member not in archive.names:
            raise ExtractionError("Requested member is not present in this archive; use an exact listed name")
        info = archive.zip.getinfo(member)
        if info.is_dir():
            raise ExtractionError("Requested archive member is a directory, not a document")
        suffix = PurePosixPath(member.lower()).suffix
        if suffix in ARCHIVE_EXTENSIONS:
            raise ExtractionError("Nested archives are unsupported; select a document or source-code member")
        member_data = archive.read(member)
        if member_data.startswith((b"PK\x03\x04", b"PK\x05\x06")):
            nested = _Archive(member_data)
            try:
                if not _packaged_document(nested):
                    raise ExtractionError(
                        "Nested archives are unsupported; select a document or source-code member"
                    )
            finally:
                nested.zip.close()
        output = extract_document(member_data, member)
        output["archive_member"] = member
        for segment in output["segments"]:
            segment["archive_member"] = member
        return output
    except (
        ExtractionError,
        zipfile.BadZipFile,
        zlib.error,
        UnicodeError,
        KeyError,
        ValueError,
        OSError,
        RuntimeError,
        NotImplementedError,
    ) as exc:
        output = result.finish(str(exc)[:400])
        if member is not None:
            output["archive_member"] = member
        return output
    finally:
        if archive is not None:
            archive.zip.close()


def _paragraph_text(element: ET.Element) -> str:
    parts = []
    for node in element.iter():
        tag = _local(node.tag)
        if tag in {"t", "text"} and node.text:
            parts.append(node.text)
        elif tag == "tab":
            parts.append("\t")
        elif tag in {"br", "cr", "lineBreak"}:
            parts.append("\n")
    return "".join(parts)


def _docx(archive: _Archive, result: _Result) -> None:
    root = archive.xml("word/document.xml")
    for index, p in enumerate((n for n in root.iter() if _local(n.tag) == "p"), 1):
        result.add(f"paragraph:{index}", _paragraph_text(p))
    for label, member in (("footnote", "word/footnotes.xml"), ("endnote", "word/endnotes.xml")):
        if member in archive.names:
            for note in archive.xml(member):
                note_id = next((v for k, v in note.attrib.items() if _local(k) == "id"), "?")
                if note_id.startswith("-") or note_id == "0":
                    continue
                text = "\n".join(_paragraph_text(n) for n in note.iter() if _local(n.tag) == "p")
                result.add(f"{label}:{note_id}", text)
    result.warnings.append("DOCX locators use paragraph order; pagination and image text are unavailable.")


def _numbered_paths(names: set[str], pattern: str) -> list[str]:
    return sorted(
        (p for p in names if re.fullmatch(pattern, p)), key=lambda p: int(re.findall(r"\d+", p)[-1])
    )


def _relationships(archive: _Archive, path: str, base: str) -> dict[str, str]:
    if path not in archive.names:
        return {}
    relationships = {}
    for rel in archive.xml(path):
        if rel.attrib.get("TargetMode") == "External":
            continue
        target = rel.attrib.get("Target", "")
        if not target or ":" in target or "\\" in target:
            continue
        resolved = posixpath.normpath(
            target.lstrip("/") if target.startswith("/") else posixpath.join(base, target)
        )
        if resolved.startswith("../") or resolved not in archive.names:
            continue
        relationships[rel.attrib.get("Id", "")] = resolved
    return relationships


def _pptx(archive: _Archive, result: _Result) -> None:
    # Presentation order can differ from slide filenames after slides are moved.
    ordered = []
    if "ppt/presentation.xml" in archive.names:
        rels = _relationships(archive, "ppt/_rels/presentation.xml.rels", "ppt")
        for element in archive.xml("ppt/presentation.xml").iter():
            if _local(element.tag) == "sldId":
                rid = next((v for k, v in element.attrib.items() if k.endswith("}id")), None)
                if rid in rels:
                    ordered.append(rels[rid])
    paths = ordered or _numbered_paths(archive.names, r"ppt/slides/slide\d+\.xml")
    for index, path in enumerate(paths, 1):
        root = archive.xml(path)
        text = "\n".join(_paragraph_text(n) for n in root.iter() if _local(n.tag) == "p")
        result.add(f"slide:{index}", text)
        relpath = f"ppt/slides/_rels/{posixpath.basename(path)}.rels"
        for target in _relationships(archive, relpath, "ppt/slides").values():
            if re.fullmatch(r"ppt/notesSlides/notesSlide\d+\.xml", target):
                notes = "\n".join(
                    _paragraph_text(n) for n in archive.xml(target).iter() if _local(n.tag) == "p"
                )
                result.add(f"slide:{index}:notes", notes)
    result.warnings.append("Slide images, charts and equations may contain text that is not extracted.")


def _xlsx(archive: _Archive, result: _Result) -> None:
    shared = []
    if "xl/sharedStrings.xml" in archive.names:
        for item in archive.xml("xl/sharedStrings.xml"):
            shared.append("".join(n.text or "" for n in item.iter() if _local(n.tag) == "t"))
    rels = _relationships(archive, "xl/_rels/workbook.xml.rels", "xl")
    workbook = archive.xml("xl/workbook.xml")
    for sheet in (n for n in workbook.iter() if _local(n.tag) == "sheet"):
        name = sheet.attrib.get("name", "Sheet")
        rid = next((v for k, v in sheet.attrib.items() if k.endswith("}id")), "")
        path = rels.get(rid)
        if not path:
            result.warnings.append(f"Sheet '{name}' has no readable internal relationship.")
            continue
        for cell in (n for n in archive.xml(path).iter() if _local(n.tag) == "c"):
            ref = cell.attrib.get("r", "?")
            kind = cell.attrib.get("t")
            value_node = next((n for n in cell if _local(n.tag) == "v"), None)
            formula_node = next((n for n in cell if _local(n.tag) == "f"), None)
            value = value_node.text or "" if value_node is not None else ""
            if kind == "s" and value:
                try:
                    shared_index = int(value)
                    if shared_index < 0:
                        raise ValueError()
                    value = shared[shared_index]
                except (ValueError, IndexError):
                    result.warnings.append(f"Invalid shared string index at {name}!{ref}.")
                    value = "[unreadable shared string]"
            elif kind == "inlineStr":
                value = "".join(n.text or "" for n in cell.iter() if _local(n.tag) == "t")
            elif kind == "b":
                value = "TRUE" if value == "1" else "FALSE"
            metadata: dict[str, Any] = {"sheet": name, "cell": ref}
            if formula_node is not None:
                formula = formula_node.text or ""
                metadata["formula"] = formula
                metadata["cached_value"] = value or None
                value = (
                    f"{value} [formula: ={formula}; cached result]"
                    if value
                    else f"[formula: ={formula}; no cached result]"
                )
            result.add(f"sheet:{name}!{ref}", value, **metadata)
    result.warnings.append(
        "Spreadsheet formulas are not executed; cached values may be stale. Numeric date cells retain Excel serial values."
    )


def _hwpx(archive: _Archive, result: _Result) -> None:
    paths = _numbered_paths(archive.names, r"Contents/section\d+\.xml")
    if not paths:
        raise ExtractionError("HWPX has no readable section XML")
    for section_index, path in enumerate(paths, 1):
        root = archive.xml(path)
        paragraphs = [n for n in root.iter() if _local(n.tag) == "p"]
        # Nested table paragraphs are extracted independently without repeating
        # their text in the enclosing paragraph.
        for paragraph_index, paragraph in enumerate(paragraphs, 1):

            def text_without_nested_p(node: ET.Element) -> str:
                parts = []
                if _local(node.tag) in {"t", "text"} and node.text:
                    parts.append(node.text)
                for child in node:
                    if _local(child.tag) != "p":
                        parts.append(text_without_nested_p(child))
                    if child.tail and _local(node.tag) in {"t", "text"}:
                        parts.append(child.tail)
                return "".join(parts)

            result.add(
                f"section:{section_index}:paragraph:{paragraph_index}", text_without_nested_p(paragraph)
            )
    result.warnings.append(
        "HWPX locators use section/paragraph order; embedded images and layout are not OCRed."
    )


def _inflate_bounded(data: bytes) -> bytes:
    inflater = zlib.decompressobj(-15)
    expanded = inflater.decompress(data, MAX_MEMBER_BYTES + 1)
    if len(expanded) > MAX_MEMBER_BYTES or inflater.unconsumed_tail:
        raise ExtractionError("HWP section expanded size limit exceeded")
    if not inflater.eof:
        raise ExtractionError("HWP compressed section is incomplete")
    return expanded


def _hwp(data: bytes, result: _Result) -> None:
    try:
        import olefile
    except ImportError:
        raise ExtractionError(
            "Legacy HWP needs the optional 'olefile' package; HWPX needs no extra package"
        ) from None
    with olefile.OleFileIO(io.BytesIO(data)) as ole:
        if not ole.exists("FileHeader"):
            raise ExtractionError("OLE document is not a supported HWP file")
        header = ole.openstream("FileHeader").read(256)
        if len(header) < 40 or not header.startswith(b"HWP Document File"):
            raise ExtractionError("Invalid HWP FileHeader")
        flags = struct.unpack_from("<I", header, 36)[0]
        if flags & (2 | 4):
            raise ExtractionError("Password-protected or distribution-protected HWP is unsupported")
        sections = sorted(
            (
                p
                for p in ole.listdir()
                if len(p) == 2 and p[0] == "BodyText" and re.fullmatch(r"Section\d+", p[1])
            ),
            key=lambda p: int(p[1][7:]),
        )
        total = 0
        for section_index, path in enumerate(sections, 1):
            raw = ole.openstream(path).read(MAX_MEMBER_BYTES + 1)
            if len(raw) > MAX_MEMBER_BYTES:
                raise ExtractionError("HWP section size limit exceeded")
            raw = _inflate_bounded(raw) if flags & 1 else raw
            total += len(raw)
            if total > MAX_EXPANDED_BYTES:
                raise ExtractionError("HWP total expanded size limit exceeded")
            offset = 0
            paragraph_index = 0
            while offset + 4 <= len(raw):
                record = struct.unpack_from("<I", raw, offset)[0]
                offset += 4
                tag, size = record & 0x3FF, record >> 20
                if size == 0xFFF:
                    if offset + 4 > len(raw):
                        raise ExtractionError("Truncated HWP record")
                    size = struct.unpack_from("<I", raw, offset)[0]
                    offset += 4
                if offset + size > len(raw):
                    raise ExtractionError("Truncated HWP record payload")
                if tag == 67:  # HWPTAG_PARA_TEXT
                    paragraph_index += 1
                    units = struct.unpack(f"<{size // 2}H", raw[offset : offset + size - size % 2])
                    chars, index = [], 0
                    while index < len(units):
                        unit = units[index]
                        if unit == 9:
                            chars.append("\t")
                        elif unit in {10, 13}:
                            chars.append("\n")
                        elif unit >= 32:
                            chars.append(chr(unit))
                        # HWP inline/extended control codes occupy eight UTF-16 units.
                        index += (
                            8
                            if unit in {1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12, 14, 15, 16, 17, 18, 21, 22, 23}
                            else 1
                        )
                    text = "".join(chars).encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")
                    result.add(f"section:{section_index}:paragraph:{paragraph_index}", text)
                offset += size
    result.warnings.append(
        "Legacy HWP extraction is best effort; embedded images, equations and layout may be missing."
    )


class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in {"script", "style", "noscript", "template"}:
            self.hidden += 1
        if not self.hidden and tag in {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "section"}:
            self.parts.append("\n")
        if not self.hidden and tag in {"td", "th"}:
            self.parts.append("\t")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "template"}:
            self.hidden = max(0, self.hidden - 1)
        if not self.hidden and tag in {"p", "div", "li", "tr", "section"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def _decode(data: bytes, result: _Result) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    for encoding in ("utf-8-sig", "cp949"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            pass
    result.warnings.append("Unknown text encoding; undecodable bytes were replaced.")
    return data.decode("utf-8", "replace")


def extract_document(data: bytes, filename: str, content_type: str | None = None) -> dict[str, Any]:
    """Extract a downloaded attachment without writing or opening local files.

    Returns status (ok/no_text/archive/error), full bounded text, source-located segments,
    warnings, and truncated. Never fetches embedded URLs or runs document content.
    """
    safe_name = _clean(str(filename).replace("\\", "/").rsplit("/", 1)[-1])[:255]
    suffix = PurePosixPath(safe_name.lower()).suffix
    kind = suffix.lstrip(".") or "unknown"
    result = _Result(safe_name, kind)
    if not isinstance(data, bytes):
        return result.finish("Document data must be bytes")
    if len(data) > MAX_INPUT_BYTES:
        return result.finish(f"Document exceeds {MAX_INPUT_BYTES // (1024 * 1024)} MiB input limit")
    try:
        if data.startswith(b"%PDF-"):
            result.kind = "pdf"
            try:
                from pypdf import PdfReader
            except ImportError:
                raise ExtractionError("PDF extraction requires the 'pypdf' package") from None
            reader = PdfReader(io.BytesIO(data), strict=False)
            if reader.is_encrypted and not reader.decrypt(""):
                raise ExtractionError("Password-protected PDF cannot be extracted")
            if len(reader.pages) > 1500:
                raise ExtractionError("PDF exceeds 1500 page limit")
            for index, page in enumerate(reader.pages, 1):
                if result.truncated:
                    break
                result.add(f"page:{index}", page.extract_text() or "")
            result.warnings.append(
                "PDF reading order can differ from visual layout; image-only text requires OCR."
            )
        elif data.startswith((b"PK\x03\x04", b"PK\x05\x06")):
            archive = _Archive(data)
            try:
                if "word/document.xml" in archive.names:
                    result.kind = "docx"
                    _docx(archive, result)
                elif "ppt/presentation.xml" in archive.names:
                    result.kind = "pptx"
                    _pptx(archive, result)
                elif "xl/workbook.xml" in archive.names:
                    result.kind = "xlsx"
                    _xlsx(archive, result)
                elif any(re.fullmatch(r"Contents/section\d+\.xml", p) for p in archive.names):
                    result.kind = "hwpx"
                    _hwpx(archive, result)
                else:
                    return _archive_listing(archive, safe_name)
            finally:
                archive.zip.close()
        elif data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
            result.kind = "hwp"
            _hwp(data, result)
        elif (
            suffix in TEXT_EXTENSIONS
            or safe_name.lower() in TEXT_FILENAMES
            or (content_type or "").split(";", 1)[0]
            in {"text/plain", "text/html", "text/csv", "application/json", "application/xml", "text/xml"}
        ):
            if b"\x00" in data and not data.startswith((b"\xff\xfe", b"\xfe\xff")):
                raise ExtractionError("Binary data cannot be read as a text/source-code document")
            text = _decode(data, result)
            if suffix in {".html", ".htm"} or (content_type or "").startswith("text/html"):
                parser = _HTMLText()
                parser.feed(text)
                text = "".join(parser.parts)
                result.kind = "html"
            for index, line in enumerate(text.splitlines(), 1):
                result.add(f"line:{index}", line)
        else:
            raise ExtractionError(
                "Unsupported attachment format; supported: PDF, DOCX, PPTX, XLSX, HWPX, HWP, ZIP listings and text/source code/HTML"
            )
    except (
        ExtractionError,
        ET.ParseError,
        zipfile.BadZipFile,
        zlib.error,
        UnicodeError,
        KeyError,
        ValueError,
        OSError,
        RuntimeError,
        RecursionError,
        struct.error,
    ) as exc:
        # Keep partial evidence if a later page/section fails, but mark it incomplete.
        if result.segments:
            result.truncated = True
        return result.finish(str(exc)[:400])
    except Exception as exc:  # noqa: BLE001 - boundary for third-party document parsers
        # Third-party PDF/OLE libraries have their own exception hierarchies.
        if result.segments:
            result.truncated = True
        return result.finish(f"Document parser failed ({type(exc).__name__})")
    return result.finish()
