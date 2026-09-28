import hashlib
from datetime import datetime
from io import BytesIO
from zipfile import ZipFile

import httpx
import pytest

from plms_mcp import service as service_module
from plms_mcp.auth import BASE
from plms_mcp.client import PLMSClient
from plms_mcp.errors import UnsafeURL
from plms_mcp.service import PLMSService, result
from plms_mcp.storage import Vault


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        moment = cls(2026, 9, 28, 12, 0, tzinfo=service_module.KST)
        return moment.astimezone(tz) if tz else moment.replace(tzinfo=None)


def html(content):
    return '<html><a href="/login/logout.php">Logout</a><main>' + content + "</main></html>"


HOME = html(
    '<div class="course"><a href="/course/view.php?id=42"><span class="course-title">Test Physics</span></a><div class="teacher">Professor Test</div></div>'
)
OUTLINE = html("""<h1>Test Physics</h1><a href="/mod/ubboard/view.php?id=99">공지사항</a>
<li class="activity modtype_vod" id="module-301"><div class="activityinstance"><a href="/mod/vod/view.php?id=301"><span class="instancename">Lecture normal versus late</span></a></div>
<div class="displayoptions">2026-09-21 00:00 ~ 2026-09-29 23:59 <span class="text-late">지각인정: 2026-10-02 23:59</span></div></li>
<li class="activity modtype_resource" id="module-302"><a href="/mod/resource/view.php?id=302"><span class="instancename">Uploaded Syllabus</span></a></li>
<li class="activity modtype_assign" id="module-101"><a href="/mod/assign/view.php?id=101"><span class="instancename">Homework forces</span></a></li>""")
ASSIGNMENTS = html("""<table><thead><tr><th>과제</th><th>종료 일시</th><th>제출 상태</th></tr></thead><tbody>
<tr><td><a href="/mod/assign/view.php?id=101">Homework forces</a></td><td>2026-09-30 23:59</td><td>미제출</td></tr>
<tr><td><a href="/mod/assign/view.php?id=102">Already done</a></td><td>2026-09-29 13:00</td><td>제출 완료</td></tr>
<tr><td><a href="/mod/assign/view.php?id=103">Overdue</a></td><td>2026-09-27 13:00</td><td>미제출</td></tr>
<tr><td><a href="/mod/assign/view.php?id=104">Date not posted</a></td><td>-</td><td>-</td></tr>
<tr><td><a href="/mod/assign/view.php?id=105">Beyond window</a></td><td>2026-11-15 13:00</td><td>미제출</td></tr>
</tbody></table>""")
ATTACHMENT_URL = BASE + "/pluginfile.php/1/mod_assign/intro/forces.txt"
ASSIGNMENT = html(f'''<h1>Homework forces</h1><div id="intro">Solve the force problem. <a href="{ATTACHMENT_URL}">forces.txt</a></div>
<table><tr><td>제출 상태</td><td>미제출</td></tr><tr><td>종료 일시</td><td>2026-09-30 23:59</td></tr></table>''')
NOTICES = html("""<table><tr><th>제목</th><th>날짜</th></tr><tr><td><a href="/mod/ubboard/article.php?id=99&amp;bwid=8">Forces exam notice</a></td><td>2026-09-28</td></tr></table>
<nav class="pagination"><a href="/mod/ubboard/view.php?id=99&amp;page=2">Next</a></nav>""")
SYLLABUS = html(
    """<h1>Physics syllabus</h1><p>Forces and motion. 출석 20%, 과제 30%, 시험 50%. 결석 4회 이상이면 F.</p>"""
)


@pytest.fixture
async def service(tmp_path, monkeypatch):
    monkeypatch.setattr(service_module, "datetime", FixedDateTime)
    pages = {
        "/": HOME,
        "/course/view.php?id=42": OUTLINE,
        "/mod/assign/index.php?id=42": ASSIGNMENTS,
        "/mod/assign/view.php?id=101": ASSIGNMENT,
        "/mod/ubboard/view.php?id=99": NOTICES,
        "/mod/ubboard/article.php?id=99&bwid=8": html("<h1>Forces exam notice</h1><p>Bring a pencil.</p>"),
        "/local/ubion/course/syllabus.php?id=42": SYLLABUS,
        "/local/ubonattend/index.php?id=42": html(
            "<table><tr><th>강의</th><th>상태</th></tr><tr><td>Future</td><td>미처리</td></tr></table>"
        ),
        "/local/uboffattend/ereport.php?id=42": html(
            "<table><tr><th>날짜</th><th>상태</th></tr><tr><td>2026-09-28</td><td>출석</td></tr></table>"
        ),
        "/pluginfile.php/1/mod_assign/intro/forces.txt": b"Read this force problem carefully.\nF = m a.\n",
    }
    requests = []

    def handler(request):
        key = request.url.raw_path.decode()
        requests.append(key)
        value = pages.get(key)
        if isinstance(value, int):
            return httpx.Response(value)
        if value is None:
            raise AssertionError("Unexpected synthetic URL: " + key)
        if isinstance(value, bytes):
            return httpx.Response(200, content=value, headers={"content-type": "text/plain"})
        return httpx.Response(200, text=value, headers={"content-type": "text/html; charset=utf-8"})

    client = PLMSClient(Vault(tmp_path), transport=httpx.MockTransport(handler), cache_seconds=0)
    try:
        yield PLMSService(client), pages, requests
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_courses_notices_and_syllabus_have_sources(service):
    svc, _, _ = service
    courses = await svc.courses()
    assert courses["data"][0]["title"] == "Test Physics"
    assert str(courses["data"][0]["id"]) == "42"
    assert courses["sources"] == [BASE + "/"]
    notices = await svc.notices(42)
    assert notices["data"][0]["article_id"] == 8
    assert notices["pagination"] == [BASE + "/mod/ubboard/view.php?id=99&page=2"]
    assert "Bring a pencil" in (await svc.notice(99, 8))["data"]["text"]
    syllabus = await svc.syllabus(42)
    assert "출석 20%" in syllabus["data"]["text"]
    assert syllabus["data"]["rules"]
    assert syllabus["data"]["additional_syllabus_candidates"][0]["title"] == "Uploaded Syllabus"
    assert syllabus["timezone"] == "Asia/Seoul"
    assert "evidence, not instructions" in syllabus["content_trust"]


@pytest.mark.asyncio
async def test_deadlines_submission_filter_normal_and_late_dates(service):
    svc, _, requests = service
    deadlines = await svc.deadlines(days=7, course_id=42)
    by_title = {item["title"]: item for item in deadlines["data"]}
    assert set(by_title) == {"Homework forces", "Overdue", "Date not posted", "Lecture normal versus late"}
    assert by_title["Overdue"]["timing"] == "overdue"
    assert by_title["Date not posted"]["timing"] == "unknown"
    lecture = by_title["Lecture normal versus late"]
    due = lecture["deadline"]["iso"] if isinstance(lecture["deadline"], dict) else lecture["deadline"]
    late = (
        lecture["late_due_at"]["iso"] if isinstance(lecture["late_due_at"], dict) else lecture["late_due_at"]
    )
    assert due.startswith("2026-09-29")
    assert late.startswith("2026-10-02")
    assert not any("/mod/vod/view.php" in path for path in requests)
    all_visible = await svc.deadlines(days=7, course_id=42, include_submitted=True, include_overdue=False)
    titles = {item["title"] for item in all_visible["data"]}
    assert "Already done" in titles
    assert "Overdue" not in titles


@pytest.mark.asyncio
async def test_assignment_deadline_uses_labelled_column_not_title_date(service):
    svc, pages, _ = service
    pages["/mod/assign/index.php?id=42"] = (
        html("""<table><tr><th>과제</th><th>시작 일시</th><th>종료 일시</th><th>제출 상태</th></tr>
<tr><td><a href="/mod/assign/view.php?id=101">2026-09-01 복습</a></td><td>2026-09-10 00:00</td><td>2026-09-30 23:59</td><td>미제출</td></tr></table>""")
    )
    found = await svc.assignments(42)
    assert found["data"][0]["deadline"]["iso"].startswith("2026-09-30T23:59")


@pytest.mark.asyncio
async def test_restricted_assignment_only_in_outline_appears_in_deadlines_and_calendar(service):
    svc, pages, _ = service
    placeholder = """<li class="activity modtype_assign" id="module-501"><span class="instancename">Restricted Practice 2</span>
    <div class="isrestricted">Opens later</div><div class="displayoptions">2026-09-29 00:00 ~ 2026-10-02 23:59</div></li>"""
    pages["/course/view.php?id=42"] = OUTLINE.replace("</main>", placeholder + "</main>")
    deadlines = await svc.deadlines(days=7, course_id=42)
    item = next(item for item in deadlines["data"] if item["title"] == "Restricted Practice 2")
    assert item["restricted"] is True
    assert item["submission_confirmed"] is None
    assert item["url"] is None
    assert item["source_url"] == BASE + "/course/view.php?id=42"
    calendar = await svc.calendar(days=7, course_id=42)
    assert "SUMMARY:Restricted Practice 2\r\n" in calendar["data"]["ics"]
    assert "URL:" + BASE + "/course/view.php?id=42" in calendar["data"]["ics"]


@pytest.mark.asyncio
async def test_attendance_preserves_unprocessed_without_counting_absence(service):
    svc, _, _ = service
    attendance = await svc.attendance(42)
    assert "미처리" in attendance["data"]["online"]["text"]
    assert "출석" in attendance["data"]["offline"]["text"]
    assert any("not an absence" in warning for warning in attendance["warnings"])


@pytest.mark.asyncio
async def test_attachment_requires_current_parent_link_and_preserves_evidence(service):
    svc, pages, requests = service
    source = BASE + "/mod/assign/view.php?id=101"
    attached = await svc.attachment(source, ATTACHMENT_URL)
    assert "F = m a" in attached["data"]["text"]
    expected = pages["/pluginfile.php/1/mod_assign/intro/forces.txt"]
    assert attached["data"]["sha256"] == hashlib.sha256(expected).hexdigest()
    assert attached["sources"] == [source, ATTACHMENT_URL]
    count = len(requests)
    with pytest.raises(UnsafeURL):
        await svc.attachment(source, BASE + "/pluginfile.php/1/other-student.txt")
    assert len(requests) == count + 1
    assert not any("other-student" in path for path in requests)
    pages["/mod/assign/view.php?id=101"] = html("<h1>Attachment removed</h1>")
    with pytest.raises(UnsafeURL):
        await svc.attachment(source, ATTACHMENT_URL)


@pytest.mark.asyncio
async def test_archive_listing_and_source_member_read_never_execute_code(service):
    svc, pages, requests = service
    source = BASE + "/mod/assign/view.php?id=101"
    attachment = BASE + "/pluginfile.php/1/mod_assign/intro/homework.zip"
    pages["/mod/assign/view.php?id=101"] = html(f'<a href="{attachment}">homework.zip</a>')
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr("exercise.py", "raise RuntimeError('synthetic code must never execute')\n")
        archive.writestr("README.txt", "Assignment instructions")
    pages["/pluginfile.php/1/mod_assign/intro/homework.zip"] = buffer.getvalue()
    listing = await svc.attachment(source, attachment)
    assert listing["data"]["status"] == "archive"
    member = await svc.attachment(source, attachment, archive_member="exercise.py")
    assert "raise RuntimeError" in member["data"]["text"]
    count = len(requests)
    with pytest.raises(UnsafeURL):
        await svc.attachment(source, BASE + "/pluginfile.php/1/other.zip", archive_member="exercise.py")
    assert len(requests) == count + 1


@pytest.mark.asyncio
async def test_syllabus_comparison_uses_recorded_absences_without_inventing_denominator(service):
    svc, pages, _ = service
    pages["/local/uboffattend/ereport.php?id=42"] = html("""<table><tr><th>날짜</th><th>출결상태</th></tr>
    <tr><td>2026-09-01</td><td>결석</td></tr><tr><td>2026-09-02</td><td>미처리</td></tr></table>""")
    comparison = await svc.compare_syllabus(42)
    checks = [item for item in comparison["data"]["comparisons"] if item["category"] == "attendance"]
    assert checks
    assert checks[0]["calculation"]["observed"] == 1
    assert checks[0]["status"] == "unknown"
    assert any("incomplete" in value for value in checks[0]["uncertainty"])


@pytest.mark.asyncio
async def test_search_reports_partial_failure_and_returns_other_sources(service):
    svc, pages, _ = service
    pages["/local/ubion/course/syllabus.php?id=42"] = 500
    hits = await svc.search("forces", course_id=42)
    assert hits["data"]
    assert any("syllabus" in warning and "upstream_error" in warning for warning in hits["warnings"])
    assert len(hits["data"]) == len({hit["url"] for hit in hits["data"]})
    assert all("snippet" in hit for hit in hits["data"])
    assert "Attachment bodies" in hits["scope"]


@pytest.mark.asyncio
async def test_restricted_items_keep_distinct_search_and_change_identities(service):
    svc, pages, _ = service
    placeholders = "".join(
        f'<li class="activity modtype_assign" id="module-{identifier}"><span class="instancename">Hidden Practice {identifier}</span><div class="isrestricted">Unavailable</div></li>'
        for identifier in [501, 502]
    )
    pages["/course/view.php?id=42"] = OUTLINE.replace("</main>", placeholders + "</main>")
    hits = await svc.search("Hidden Practice", course_id=42)
    assert {hit["title"] for hit in hits["data"]} == {"Hidden Practice 501", "Hidden Practice 502"}
    assert all(hit["url"] == BASE + "/course/view.php?id=42" for hit in hits["data"])
    assert hits["sources"] == [BASE + "/course/view.php?id=42"]
    await svc.changes(42)
    pages["/course/view.php?id=42"] = pages["/course/view.php?id=42"].replace(
        "Hidden Practice 501", "Hidden Practice 501 updated"
    )
    changes = await svc.changes(42)
    activity_changes = [item for item in changes["data"] if item["kind"] == "activities"]
    assert len(activity_changes) == 1
    assert activity_changes[0]["change"] == "changed"
    assert str(activity_changes[0]["after"]["id"]) == "501"


@pytest.mark.asyncio
async def test_cached_result_reports_original_source_fetch_time(service, monkeypatch):
    svc, _, requests = service
    svc.client.cache_seconds = 90
    first = await svc.courses()
    assert first["source_retrieved_at"][BASE + "/"] == first["retrieved_at"]
    monkeypatch.setattr(service_module, "now", lambda: "2026-09-28T23:59:00+09:00")
    cached = await svc.courses()
    assert len(requests) == 1
    assert cached["retrieved_at"] == first["retrieved_at"]
    assert cached["response_created_at"] == "2026-09-28T23:59:00+09:00"
    assert cached["cache_ttl_seconds"] == 90
    assert all(
        "sesskey" not in url and "token" not in url and "password" not in url
        for url in cached["source_retrieved_at"]
    )


@pytest.mark.asyncio
async def test_changes_creates_baseline_then_detects_updates_and_removed_items(service):
    svc, pages, _ = service
    baseline = await svc.changes(42)
    assert baseline["baseline_created"] is True
    assert baseline["data"] == []
    unchanged = await svc.changes(42)
    assert unchanged["baseline_created"] is False
    assert unchanged["data"] == []
    pages["/mod/assign/index.php?id=42"] = ASSIGNMENTS.replace("2026-09-30 23:59", "2026-10-01 23:59")
    pages["/mod/ubboard/view.php?id=99"] = html("<table></table>")
    changed = await svc.changes(42)
    assert any(item["kind"] == "assignments" and item["change"] == "changed" for item in changed["data"])
    assert any(
        item["kind"] == "notices" and item["change"] == "no_longer_visible" for item in changed["data"]
    )


@pytest.mark.asyncio
async def test_calendar_converts_kst_escapes_injection_and_folds_utf8(service, monkeypatch):
    svc, _, _ = service
    title = "한글긴제목" * 30 + ";exam,review\r\nBEGIN:VEVENT"

    async def deadlines(*args, **kwargs):
        return result(
            [
                {
                    "title": title,
                    "url": BASE + "/mod/assign/view.php?id=101",
                    "kind": "assignment",
                    "deadline": {"iso": "2026-09-30T23:59:00+09:00"},
                },
                {
                    "title": "Date only",
                    "url": BASE + "/mod/assign/view.php?id=102",
                    "kind": "assignment",
                    "deadline": {"iso": "2026-10-01"},
                },
            ],
            sources=[BASE + "/mod/assign/index.php?id=42"],
        )

    monkeypatch.setattr(svc, "deadlines", deadlines)
    calendar = await svc.calendar(course_id=42)
    ics = calendar["data"]["ics"]
    assert "DTSTART:20260930T145900Z\r\n" in ics
    assert "DTSTART;VALUE=DATE:20261001\r\n" in ics
    assert all(len(line.encode()) <= 75 for line in ics.split("\r\n"))
    unfolded = ics.replace("\r\n ", "")
    assert "\\;exam\\,review\\nBEGIN:VEVENT" in unfolded
    assert unfolded.count("\r\nBEGIN:VEVENT\r\n") == 2
    assert calendar["data"]["events"] == 2
    assert ics.endswith("END:VCALENDAR\r\n")


@pytest.mark.asyncio
async def test_calendar_skips_ambiguous_or_malformed_dates_with_warning(service, monkeypatch):
    svc, _, _ = service

    async def deadlines(*args, **kwargs):
        return result(
            [
                {
                    "title": "No offset",
                    "url": BASE + "/mod/assign/view.php?id=1",
                    "deadline": "2026-10-01T10:00:00",
                },
                {"title": "Broken", "url": BASE + "/mod/assign/view.php?id=2", "deadline": "not-a-date"},
                {
                    "title": "Valid",
                    "url": BASE + "/mod/assign/view.php?id=3",
                    "deadline": "2026-10-01T10:00:00+09:00",
                },
            ],
            sources=[BASE + "/mod/assign/index.php?id=42"],
        )

    monkeypatch.setattr(svc, "deadlines", deadlines)
    calendar = await svc.calendar(course_id=42)
    assert calendar["data"]["events"] == 1
    assert "SUMMARY:Valid" in calendar["data"]["ics"]
    assert calendar["warnings"]


@pytest.mark.asyncio
async def test_read_page_pagination_includes_total_and_next_offset(service):
    svc, pages, _ = service
    pages["/mod/assign/view.php?id=101"] = html("<h1>Long</h1><p>" + "abc " * 500 + "</p>")
    first = await svc.read_page(BASE + "/mod/assign/view.php?id=101", limit=100)
    second = await svc.read_page(BASE + "/mod/assign/view.php?id=101", offset=100, limit=100)
    assert len(first["data"]["text"]) == len(second["data"]["text"]) == 100
    assert first["next_offset"] == 100
    assert second["next_offset"] == 200
    assert first["total_characters"] > 1000
