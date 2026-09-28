import io
import zipfile

import pytest
from pypdf import PdfWriter

from plms_mcp.extraction import extract_archive, extract_document


def archive(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for name, content in files.items():
            z.writestr(name, content)
    return buffer.getvalue()


def test_docx_paragraphs_and_footnotes_have_locators():
    data = archive(
        {
            "word/document.xml": '<w:document xmlns:w="urn:w"><w:body><w:p><w:r><w:t>과제</w:t><w:tab/><w:t>마감</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>표 내부</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>',
            "word/footnotes.xml": '<w:footnotes xmlns:w="urn:w"><w:footnote w:id="1"><w:p><w:r><w:t>추가 규칙</w:t></w:r></w:p></w:footnote></w:footnotes>',
        }
    )
    result = extract_document(data, "syllabus.docx")
    assert result["status"] == "ok"
    assert result["segments"] == [
        {"locator": "paragraph:1", "text": "과제\t마감"},
        {"locator": "paragraph:2", "text": "표 내부"},
        {"locator": "footnote:1", "text": "추가 규칙"},
    ]


def test_pptx_follows_presentation_order_and_notes():
    data = archive(
        {
            "ppt/presentation.xml": '<p:presentation xmlns:p="urn:p" xmlns:r="urn:r"><p:sldIdLst><p:sldId id="1" r:id="r2"/><p:sldId id="2" r:id="r1"/></p:sldIdLst></p:presentation>',
            "ppt/_rels/presentation.xml.rels": '<Relationships><Relationship Id="r1" Target="slides/slide1.xml"/><Relationship Id="r2" Target="slides/slide2.xml"/></Relationships>',
            "ppt/slides/slide1.xml": "<slide><p><t>Second</t></p></slide>",
            "ppt/slides/slide2.xml": "<slide><p><t>First</t></p></slide>",
            "ppt/slides/_rels/slide2.xml.rels": '<Relationships><Relationship Id="n" Target="../notesSlides/notesSlide2.xml"/></Relationships>',
            "ppt/notesSlides/notesSlide2.xml": "<notes><p><t>Speaker evidence</t></p></notes>",
        }
    )
    result = extract_document(data, "lecture.pptx")
    assert [(s["locator"], s["text"]) for s in result["segments"]] == [
        ("slide:1", "First"),
        ("slide:1:notes", "Speaker evidence"),
        ("slide:2", "Second"),
    ]


def test_xlsx_preserves_sheet_cell_formula_and_does_not_execute():
    data = archive(
        {
            "xl/workbook.xml": '<workbook xmlns:r="urn:r"><sheets><sheet name="출석표" r:id="r1"/></sheets></workbook>',
            "xl/_rels/workbook.xml.rels": '<Relationships><Relationship Id="r1" Target="worksheets/sheet1.xml"/></Relationships>',
            "xl/sharedStrings.xml": "<sst><si><t>홍길동</t></si></sst>",
            "xl/worksheets/sheet1.xml": '<worksheet><sheetData><row><c r="A1" t="s"><v>0</v></c><c r="B1"><f>SUM(1,2)</f><v>3</v></c><c r="C1"><f>WEBSERVICE("https://example.invalid")</f></c><c r="D1" t="inlineStr"><is><t>결석</t></is></c></row></sheetData></worksheet>',
        }
    )
    result = extract_document(data, "roster.xlsx")
    assert result["segments"][0]["locator"] == "sheet:출석표!A1"
    assert result["segments"][0]["text"] == "홍길동"
    assert result["segments"][1]["cached_value"] == "3"
    assert result["segments"][2]["cached_value"] is None
    assert "no cached result" in result["segments"][2]["text"]


def test_hwpx_does_not_duplicate_nested_table_paragraphs():
    data = archive(
        {
            "Contents/section0.xml": "<section><p><run><t>제출 지침</t><table><p><run><t>표 내용</t></run></p></table></run></p></section>"
        }
    )
    result = extract_document(data, "guide.hwpx")
    assert [s["text"] for s in result["segments"]] == ["제출 지침", "표 내용"]
    assert result["text"].count("표 내용") == 1


def test_html_ignores_script_style_and_preserves_unicode():
    result = extract_document(
        "<h1>공지</h1><script>steal()</script><style>hidden</style><p>기한 &amp; 제출</p>".encode(),
        "notice.html",
    )
    assert "공지" in result["text"] and "기한 & 제출" in result["text"]
    assert "steal" not in result["text"] and "hidden" not in result["text"]


def test_korean_legacy_encoding():
    result = extract_document("과제 마감\n9월 28일".encode("cp949"), "notice.txt")
    assert result["segments"][1] == {"locator": "line:2", "text": "9월 28일"}


@pytest.mark.parametrize("name", ["../private.xml", "/private.xml", "word\\private.xml"])
def test_zip_traversal_rejected(name):
    result = extract_document(archive({name: "x"}), "unsafe.docx")
    assert result["status"] == "error"
    assert "unsafe member path" in result["error"]


def test_zip_bomb_rejected_before_parsing():
    result = extract_document(archive({"word/document.xml": "a" * (2 * 1024 * 1024)}), "bomb.docx")
    assert result["status"] == "error"
    assert "compression ratio" in result["error"]


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])
def test_xml_entities_rejected_even_in_utf16(encoding):
    xml = '<!DOCTYPE document [<!ENTITY value "injected">]><document><p><t>&value;</t></p></document>'
    result = extract_document(archive({"word/document.xml": xml.encode(encoding)}), "entities.docx")
    assert result["status"] == "error"
    assert "declarations" in result["error"]


def test_pdf_blank_and_password_protected_are_explicit():
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    data = io.BytesIO()
    writer.write(data)
    result = extract_document(data.getvalue(), "scan.pdf")
    assert result["status"] == "no_text"
    assert any("OCR" in w for w in result["warnings"])
    writer.encrypt("synthetic-password")
    data = io.BytesIO()
    writer.write(data)
    result = extract_document(data.getvalue(), "locked.pdf")
    assert result["status"] == "error"
    assert "Password-protected" in result["error"]


def test_actual_pdf_text_has_page_locator():
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=200)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
    )
    content = DecodedStreamObject()
    content.set_data(b"BT /F1 12 Tf 10 100 Td (Deadline evidence) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(content)
    buffer = io.BytesIO()
    writer.write(buffer)
    result = extract_document(buffer.getvalue(), "proof.pdf")
    assert result["segments"] == [{"locator": "page:1", "text": "Deadline evidence"}]


def test_unsupported_binary_never_decoded_as_text():
    result = extract_document(b"\x89PNG\r\n\x1a\n", "diagram.png")
    assert result["status"] == "error"
    assert result["text"] == ""


def test_input_and_text_limits(monkeypatch):
    import plms_mcp.extraction as module

    monkeypatch.setattr(module, "MAX_INPUT_BYTES", 20)
    assert extract_document(b"a" * 21, "large.txt")["status"] == "error"
    monkeypatch.setattr(module, "MAX_TEXT_CHARS", 4)
    result = extract_document(b"abcdef", "short.txt")
    assert result["text"] == "abcd" and result["truncated"]


def test_zip_listing_is_useful_without_extracting_members():
    data = archive(
        {"assign2/": b"", "assign2/main.c": b"int main(void) { return 0; }", "instructions.txt": "제출 안내"}
    )
    result = extract_document(data, "assign2.zip")
    assert result["status"] == "archive"
    assert result["filename"] == "assign2.zip"
    assert result["member_count"] == 3
    assert "assign2/main.c" in result["text"]
    entry = result["entries"][1]
    assert entry["extension"] == ".c" and entry["readable_document"]
    assert entry["uncompressed_bytes"] == len(b"int main(void) { return 0; }")
    assert result["entries"][0]["is_directory"]


def test_zip_selected_source_code_keeps_member_and_line_evidence():
    data = archive({"task/main.py": "# 과제 코드\nraise RuntimeError('must never run')\n"})
    result = extract_archive(data, "task/main.py")
    assert result["status"] == "ok"
    assert result["archive_member"] == "task/main.py"
    assert result["segments"][1]["locator"] == "line:2"
    assert result["segments"][1]["archive_member"] == "task/main.py"
    assert "must never run" in result["text"]


def test_zip_selected_packaged_document_is_readable():
    docx = archive({"word/document.xml": "<document><p><t>과제 설명</t></p></document>"})
    result = extract_archive(archive({"guide.docx": docx}), "guide.docx")
    assert result["status"] == "ok"
    assert result["format"] == "docx"
    assert result["text"] == "과제 설명"


@pytest.mark.parametrize("member", ["nested.zip", "disguised.txt"])
def test_zip_nested_general_archive_blocked_even_when_renamed(member):
    result = extract_archive(archive({member: archive({"inner.txt": "content"})}), member)
    assert result["status"] == "error"
    assert "Nested archives" in result["error"]


def test_zip_invalid_selection_and_empty_archive_are_explicit():
    data = archive({"folder/": b"", "folder/readme.txt": "instructions"})
    assert "exact listed name" in extract_archive(data, "readme.txt")["error"]
    assert "directory" in extract_archive(data, "folder/")["error"]
    assert extract_archive(data, "../outside")["status"] == "error"
    assert extract_archive(archive({}))["status"] == "archive"
    assert extract_document(archive({}), "empty.zip")["entries"] == []


def test_zip_encrypted_flag_rejected_before_listing():
    data = bytearray(archive({"readme.txt": "secret"}))
    local = data.index(b"PK\x03\x04")
    central = data.index(b"PK\x01\x02")
    data[local + 6] |= 1
    data[central + 8] |= 1
    result = extract_archive(bytes(data))
    assert result["status"] == "error"
    assert "Encrypted" in result["error"]


def test_zip_symbolic_link_rejected_without_following():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zipped:
        info = zipfile.ZipInfo("danger.py")
        info.create_system = 3
        info.external_attr = 0o120777 << 16
        zipped.writestr(info, "/etc/passwd")
    result = extract_archive(buffer.getvalue(), "danger.py")
    assert result["status"] == "error"
    assert "symbolic links" in result["error"]


@pytest.mark.parametrize("name", ["C:/outside.txt", "newline\nname.txt"])
def test_zip_unsafe_names_blocked_for_listing(name):
    assert extract_archive(archive({name: "value"}))["status"] == "error"


def test_zip_bomb_checks_also_apply_before_member_selection():
    data = archive({"safe.txt": "safe", "bomb.txt": b"x" * (2 * 1024 * 1024)})
    assert "compression ratio" in extract_archive(data, "safe.txt")["error"]


def test_binary_disguised_as_source_is_not_returned_as_text():
    result = extract_archive(archive({"fake.c": b"\x7fELF\x00binary"}), "fake.c")
    assert result["status"] == "error"
    assert "Binary data" in result["error"]


def test_code_extension_and_makefile_read_as_text_without_execution():
    for name in ("main.c", "header.h", "solve.py", "Makefile", "README"):
        result = extract_document(b"synthetic source code", name)
        assert result["status"] == "ok"
