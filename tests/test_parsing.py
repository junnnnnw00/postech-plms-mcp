"""Synthetic pages only: no captured student or course content belongs here."""

from datetime import date

from plms_mcp.parsing import (
    parse_activities,
    parse_announcements,
    parse_assignment,
    parse_attendance,
    parse_courses,
    parse_dates,
    parse_page,
    safe_url,
)

BASE = "https://plms.example/course/view.php?id=12"


def test_dates_preserve_precision_and_do_not_invent_year_or_time():
    values = parse_dates("2026년 9월 30일, 2026-10-02 23:59:00, 2026. 10. 3. (토) 오후 2:30")
    assert values[0]["iso"] == "2026-09-30"
    assert values[0]["precision"] == "day"
    assert values[1]["iso"] == "2026-10-02T23:59:00+09:00"
    assert values[1]["precision"] == "second"
    assert values[2]["iso"] == "2026-10-03T14:30:00+09:00"
    assert values[2]["precision"] == "minute"
    assert parse_dates("9/30, 내일, 2026-02-30, 50:18") == []


def test_english_dates_offsets_and_twelve_hour_clock():
    values = parse_dates("Tuesday, 29 September 2026, 7:00 PM; Oct 1, 2026 12:00 AM; 2026-10-01T02:00:00Z")
    assert [value["iso"] for value in values] == [
        "2026-09-29T19:00:00+09:00",
        "2026-10-01T00:00:00+09:00",
        "2026-10-01T11:00:00+09:00",
    ]


def test_visible_content_and_urls_do_not_return_session_secrets():
    html = """<title>Example</title><main><h1>Visible</h1><script>secret one</script>
      <input type="hidden" value="secret two"><textarea>secret three</textarea>
      <div hidden>secret four</div><p style="display: none">secret five</p>
      <a href="/pluginfile.php/12/material.pdf?sesskey=secret&amp;token=secret&amp;forcedownload=1">Handout.pdf</a>
      <a href="javascript:alert('secret')">Ignore link</a><p>Read me</p></main>"""
    page = parse_page(html, BASE + "&sesskey=secret")
    assert "secret" not in str(page)
    assert page["title"] == "Visible"
    assert (
        page["attachments"][0]["url"] == "https://plms.example/pluginfile.php/12/material.pdf?forcedownload=1"
    )
    assert (
        safe_url("https://name:password@plms.example/a?access_token=x&id=5#token=x", BASE)
        == "https://plms.example/a?id=5"
    )
    assert safe_url("data:text/html,private", BASE) is None


def test_course_cards_preferred_over_navigation_and_badges_removed():
    html = """<a href="/course/view.php?id=12">Navigation</a><div class="course" data-id="12">
      <a href="/course/view.php?id=12"><h4 class="coursename"><span class="badge badge-period"
      title="2026-09-01 ~ 2026-12-01">진행중</span>Sample (TEST101-01)</h4><div class="prof">Example teacher</div></a></div>"""
    courses = parse_courses(html, BASE)
    assert len(courses) == 1
    assert courses[0]["title"] == "Sample (TEST101-01)"
    assert courses[0]["code"] == "TEST101-01"
    assert len(courses[0]["period"]) == 2


def test_regular_deadline_distinguished_from_late_deadline_and_availability():
    html = """<main><li class="section"><h3 class="sectionname">Week 4</h3>
      <li class="activity vod modtype_vod" id="module-42"><div class="availabilityinfo isrestricted">
      <strong>2026년 9월 30일</strong> 부터 사용가능</div><div class="activityinstance">
      <a href="/mod/vod/view.php?id=42&amp;sesskey=private"><span class="instancename">Video<span class="accesshide">VOD</span></span></a>
      <span class="displayoptions"><span class="text-time">2026-09-28 00:00:00 ~ 2026-10-04 23:59:59
      <span class="text-late">(지각 : 2026-10-11 23:59:59)</span></span><span class="text-playtime">01:24:38</span></span>
      </div><div class="autocompletion"><img alt="미완료"></div></li></li></main>"""
    item = parse_activities(html, BASE)[0]
    assert item["title"] == "Video"
    assert item["due_at"]["iso"] == "2026-10-04T23:59:59+09:00"
    assert item["late_due_at"]["iso"] == "2026-10-11T23:59:59+09:00"
    assert item["availability"]["dates"][0]["iso"] == "2026-09-30"
    assert item["completion"]["status"] == "incomplete"
    assert item["section"] == "Week 4"
    assert item["restricted"] is True


def test_restricted_activity_is_kept_without_fabricating_clickable_link():
    html = """<li class="activity assign modtype_assign" id="module-99"><div class="availabilityinfo isrestricted">제한됨</div>
      <span class="instancename">Hidden until next week</span><span class="displayoptions">2026-11-25 00:00 ~ 2026-11-27 23:59</span></li>"""
    activity = parse_activities(html, BASE)[0]
    assert activity["id"] == "99" and activity["type"] == "assign"
    assert activity["url"] is None
    assert activity["due_at"]["iso"] == "2026-11-27T23:59:00+09:00"


def test_timeline_title_timestamp_beats_truncated_visible_day():
    html = """<div class="timeline"><a class="media" href="/mod/assign/view.php?id=42">
      <h5 class="coursename">Example</h5><h6 class="modname">Homework is due</h6>
      <div class="date" title="2026-09-29 19:00:00">2026-09-29</div></a></div>"""
    item = parse_activities(html, BASE)[0]
    assert item["due_at"]["iso"] == "2026-09-29T19:00:00+09:00"
    assert item["course_title"] == "Example"


def test_assignment_distinguishes_missing_status_from_not_submitted():
    html = """<main><h2>Homework</h2><div id="intro">Implement a queue.
      <a href="/pluginfile.php/10/guide.pdf">guide.pdf</a></div><table>
      <tr><th scope="row">제출 여부</th><td>제출 안 함</td></tr>
      <tr><th scope="row">종료 일시</th><td>2026-10-02 17:00</td></tr>
      <tr><th scope="row">마감까지 남은 기한</th><td>2 일</td></tr></table></main>"""
    item = parse_assignment(html, BASE)
    assert item["submitted"] is False
    assert item["due_at"]["iso"] == "2026-10-02T17:00:00+09:00"
    assert item["cutoff_at"] is None
    assert item["time_remaining"] == "2 일"
    assert len(item["attachments"]) == 1
    assert parse_assignment("<main><h2>Not available</h2></main>", BASE)["submitted"] is None


def test_spanning_headers_and_attendance_rows_keep_context():
    html = """<main><table><thead><tr><th rowspan="2">Week</th><th colspan="2">Attendance</th></tr>
      <tr><th>Online</th><th>Classroom</th></tr></thead><tbody>
      <tr><td rowspan="2">4</td><td>Present</td><td>Unknown</td></tr>
      <tr><td>Late</td><td>Absent</td></tr></tbody></table></main>"""
    page = parse_attendance(html, BASE)
    assert page["tables"][0]["headers"] == ["Week", "Attendance / Online", "Attendance / Classroom"]
    assert page["tables"][0]["rows"][1] == ["4", "Late", "Absent"]
    assert len(page["records"]) == 2


def test_pagination_and_announcement_links_keep_only_safe_urls():
    html = """<main><a href="/mod/ubboard/article.php?id=42&amp;bwid=7&amp;sesskey=x">Notice</a>
      <nav class="pagination"><a href="?id=42&amp;page=2&amp;token=x">Next</a></nav></main>"""
    page = parse_announcements(html, BASE)
    assert len(page["posts"]) == 1
    assert page["pagination"] == ["https://plms.example/course/view.php?id=42&page=2"]


def test_future_attendance_defaults_are_scheduled_not_absences_or_presence():
    html = """<table><thead><tr><th>수업일자</th><th>출석</th><th>결석</th><th>미처리</th></tr></thead>
      <tbody><tr><td>2026-09-20</td><td>O</td><td></td><td></td></tr>
      <tr><td>2026-09-21</td><td></td><td>O</td><td></td></tr>
      <tr><td>2026-09-30</td><td></td><td>O</td><td></td></tr>
      <tr><td>2026-10-01</td><td>O</td><td></td><td></td></tr>
      <tr><td>2026-10-02</td><td></td><td></td><td>O</td></tr>
      <tr><td>2026-09-22</td><td></td><td></td><td>O</td></tr></tbody></table>"""
    page = parse_attendance(html, BASE, as_of=date(2026, 9, 28))
    assert page["summary"]["counts"] == {"present": 1, "late": 0, "early_leave": 0, "absent": 1}
    assert page["summary"]["excluded"]["future"] == 3
    assert page["summary"]["excluded"]["unprocessed"] == 1
    assert page["records"][2]["status"] == "scheduled"
    assert page["records"][2]["recorded_status"] == "absent"
    assert page["records"][2]["countable"] is False


def test_undated_online_attendance_retains_status_but_is_not_counted():
    html = """<table><tr><th>주차</th><th>출결상태</th></tr><tr><td>4</td><td>출석</td></tr></table>"""
    page = parse_attendance(html, BASE, as_of=date(2026, 9, 28))
    assert page["records"][0]["status"] == "present"
    assert page["records"][0]["excluded_reason"] == "date_unknown"
    assert page["summary"]["counted_records"] == 0


def test_paragraphs_preserved_without_splitting_inline_policy_sentences():
    html = """<main><p>과제는 <strong>2026년 10월 2일</strong>까지 제출합니다.</p>
      <p>지각 제출은 <em>10%</em> 감점입니다.<br>결석 세 번이면 F입니다.</p>
      <table><tr><th scope="row">시험</th><td>40%</td></tr></table></main>"""
    assert parse_page(html, BASE)["text"].splitlines() == [
        "과제는 2026년 10월 2일까지 제출합니다.",
        "지각 제출은 10% 감점입니다.",
        "결석 세 번이면 F입니다.",
        "시험 | 40%",
    ]
