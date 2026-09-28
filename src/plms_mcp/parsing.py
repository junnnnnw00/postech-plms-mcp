"""Pure, conservative extraction of visible PLMS/Moodle HTML.

No requests, browser execution, or account state are used here. Dates without a
time remain dates; missing years are not inferred from the machine's clock.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup, Comment, Tag

SEOUL = ZoneInfo("Asia/Seoul")
_MONTHS = {
    name.lower(): i
    for i, name in enumerate(
        [
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ],
        1,
    )
}
_MONTHS.update({name[:3]: number for name, number in list(_MONTHS.items())})
_NUMERIC_DATE = re.compile(
    r"(?<!\d)(?P<year>(?:19|20)\d{2})\s*(?:[-./]|년)\s*"
    r"(?P<month>\d{1,2})\s*(?:[-./]|월)\s*(?P<day>\d{1,2})(?:일|\.(?!\d))?(?!\d)"
)
_MONTH_NAMES = "|".join(sorted(_MONTHS, key=len, reverse=True))
_ENGLISH_DATE = re.compile(
    rf"(?<!\w)(?:(?P<day>\d{{1,2}})\s+(?P<month>{_MONTH_NAMES})\.?\s*,?\s*"
    rf"(?P<year>(?:19|20)\d{{2}})|(?P<month2>{_MONTH_NAMES})\.?\s+"
    rf"(?P<day2>\d{{1,2}})\s*,?\s*(?P<year2>(?:19|20)\d{{2}}))(?!\d)",
    re.I,
)
_TIME_SUFFIX = re.compile(
    r"^(?:\s*\([가-힣A-Za-z. ]{1,12}\))?[,\sT]*"
    r"(?:(?P<period>오전|오후|AM|PM)\s*)?"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2})(?::(?P<second>\d{2}))?"
    r"|시(?:\s*(?P<kminute>\d{1,2})분)?(?:\s*(?P<ksecond>\d{1,2})초)?)"
    r"(?:\s*(?P<period2>AM|PM))?(?:\s*(?P<tz>Z|[+-]\d{2}:?\d{2}))?(?!\d)",
    re.I,
)
_FILE_EXT = re.compile(
    r"\.(?:pdf|hwp|hwpx|docx?|pptx?|xlsx?|csv|tsv|txt|zip|7z|rar|py|ipynb|c|cpp|java)$", re.I
)
_SECRET_KEYS = re.compile(
    r"sesskey|token|password|passwd|secret|authorization|session|api_?key|authkey|signature", re.I
)


def parse_dates(text: str) -> list[dict]:
    """Return explicit full dates, preserving day/minute/second precision.

    ``iso`` is YYYY-MM-DD at day precision, otherwise an offset-aware datetime.
    Explicit source offsets are converted to Korea time; missing offsets use
    Asia/Seoul. Yearless dates and relative expressions remain in source text.
    """
    result: list[dict] = []
    matches = sorted([*_NUMERIC_DATE.finditer(text), *_ENGLISH_DATE.finditer(text)], key=lambda m: m.start())
    previous_end = -1
    for match in matches:
        if match.start() < previous_end:
            continue
        groups = match.groupdict()
        year = int(groups.get("year") or groups.get("year2") or 0)
        month_value = groups.get("month") or groups.get("month2") or ""
        month = int(month_value) if month_value.isdigit() else _MONTHS.get(month_value.lower(), 0)
        day = int(groups.get("day") or groups.get("day2") or 0)
        suffix = _TIME_SUFFIX.match(text[match.end() :])
        end = match.end()
        precision = "day"
        try:
            value: date | datetime = date(year, month, day)
            if suffix:
                hour = int(suffix["hour"])
                minute = int(suffix["minute"] or suffix["kminute"] or 0)
                second = int(suffix["second"] or suffix["ksecond"] or 0)
                period = (suffix["period"] or suffix["period2"] or "").lower()
                if period:
                    if not 1 <= hour <= 12:
                        raise ValueError("invalid twelve-hour clock")
                    hour = hour % 12 + (12 if period in {"pm", "오후"} else 0)
                value = datetime(year, month, day, hour, minute, second, tzinfo=SEOUL)
                if suffix["tz"]:
                    offset = suffix["tz"]
                    source = datetime.fromisoformat(
                        f"{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}{offset}"
                    )
                    value = source.astimezone(SEOUL)
                end += suffix.end()
                precision = (
                    "second" if suffix["second"] is not None or suffix["ksecond"] is not None else "minute"
                )
        except ValueError:
            continue
        raw = text[match.start() : end].strip()
        item = {"raw": raw, "iso": value.isoformat(), "precision": precision, "timezone": "Asia/Seoul"}
        if item not in result:
            result.append(item)
        previous_end = end
    return result


def safe_url(href: str, base_url: str) -> str | None:
    """Resolve HTTP(S) links without reflecting credentials or token queries."""
    if not href or href.strip().startswith("#"):
        return None
    try:
        parsed = urlsplit(urljoin(base_url, href.strip()))
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return None
        host = parsed.hostname
        if ":" in host:
            host = f"[{host}]"
        netloc = host + (f":{parsed.port}" if parsed.port else "")
        query = [
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if not _SECRET_KEYS.search(key) and key.lower() not in {"key", "auth", "ticket", "code"}
        ]
        return urlunsplit((parsed.scheme.lower(), netloc, parsed.path, urlencode(query), ""))
    except (TypeError, ValueError):
        return None


def _soup(html: str) -> BeautifulSoup:
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.find_all(string=lambda value: isinstance(value, Comment)):
        node.extract()
    for node in list(soup.find_all(True)):
        if node.parent is None:
            continue
        classes = set(node.get("class", []))
        style = re.sub(r"\s+", "", node.get("style", "")).lower()
        hidden = (
            node.has_attr("hidden")
            or node.get("aria-hidden") == "true"
            or bool(classes & {"hidden", "d-none", "modal"})
            or "display:none" in style
            or "visibility:hidden" in style
        )
        if (
            node.name
            in {
                "script",
                "style",
                "noscript",
                "template",
                "input",
                "textarea",
                "select",
                "iframe",
                "object",
                "embed",
                "canvas",
                "svg",
            }
            or hidden
        ):
            node.decompose()
        elif classes & {"accesshide", "sr-only", "visually-hidden"} and "sectionname" not in classes:
            node.decompose()
    return soup


def _text(node: Tag | BeautifulSoup | None) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip() if node else ""


def _readable_text(node: Tag | BeautifulSoup | None) -> str:
    """Keep block boundaries without breaking sentences around inline tags."""
    if node is None:
        return ""
    blocks = {
        "address",
        "article",
        "aside",
        "blockquote",
        "div",
        "dl",
        "dt",
        "dd",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "ul",
    }
    pieces: list[str] = []

    def walk(current):
        if isinstance(current, str):
            pieces.append(re.sub(r"\s+", " ", current))
            return
        if not isinstance(current, Tag):
            return
        if current.name == "table":
            table = _table(current)
            pieces.append("\n")
            if table["headers"]:
                pieces.append(" | ".join(table["headers"]) + "\n")
            pieces.extend(" | ".join(row) + "\n" for row in table["rows"])
            return
        if current.name == "br":
            pieces.append("\n")
            return
        if current.name in blocks:
            pieces.append("\n")
        for child in current.children:
            walk(child)
        if current.name in blocks:
            pieces.append("\n")

    walk(node)
    return "\n".join(line.strip() for line in "".join(pieces).splitlines() if line.strip())


def _root(soup: BeautifulSoup) -> Tag | BeautifulSoup:
    return soup.select_one("#region-main, main, [role=main], #content") or soup.body or soup


def _links(root: Tag | BeautifulSoup, url: str) -> list[dict]:
    links: dict[str, dict] = {}
    for anchor in root.select("a[href]"):
        target = safe_url(str(anchor.get("href", "")), url)
        if not target:
            continue
        title = _text(anchor) or str(anchor.get("title", ""))
        if not title:
            image = anchor.find("img", alt=True)
            title = str(image.get("alt", "")) if image else ""
        item = {"title": title, "url": target}
        if target not in links or not links[target]["title"]:
            links[target] = item
    return list(links.values())


def _attachments(links: list[dict]) -> list[dict]:
    return [
        link
        for link in links
        if "/pluginfile.php/" in link["url"]
        or "/webservice/pluginfile.php/" in link["url"]
        or "/draftfile.php/" in link["url"]
        or _FILE_EXT.search(urlsplit(link["url"]).path)
        or _FILE_EXT.search(link["title"])
    ]


def _table(table: Tag) -> dict:
    """Expand row/column spans, retaining repeated group labels as context."""
    grid: dict[tuple[int, int], str] = {}
    header_rows: list[int] = []
    source_rows = [row for row in table.find_all("tr") if row.find_parent("table") is table][:1000]
    for row_index, row in enumerate(source_rows):
        cells = row.find_all(["th", "td"], recursive=False)
        is_header = (
            row.find_parent("thead") is not None
            or bool(cells)
            and all(cell.name == "th" and cell.get("scope") != "row" for cell in cells)
        )
        if is_header:
            header_rows.append(row_index)
        column = 0
        for cell in cells[:100]:
            while (row_index, column) in grid and column < 100:
                column += 1
            try:
                row_span = max(1, min(int(cell.get("rowspan", 1)), 100))
                col_span = max(1, min(int(cell.get("colspan", 1)), 100))
            except (TypeError, ValueError):
                row_span, col_span = 1, 1
            for r in range(row_index, min(row_index + row_span, len(source_rows))):
                for c in range(column, min(column + col_span, 100)):
                    grid[(r, c)] = _text(cell)
            column += col_span
    width = max((c + 1 for _, c in grid), default=0)
    headers = []
    for column in range(width):
        values = list(dict.fromkeys(grid.get((row, column), "") for row in header_rows))
        headers.append(" / ".join(value for value in values if value))
    return {
        "headers": headers if header_rows else [],
        "rows": [
            [grid.get((r, c), "") for c in range(width)]
            for r in range(len(source_rows))
            if r not in header_rows
        ],
        "caption": _text(table.find("caption")),
    }


def parse_page(html: str, url: str) -> dict:
    soup = _soup(html)
    root = _root(soup)
    heading = (
        root.select_one("h1, h2")
        or soup.select_one("#page-header h1")
        or root.select_one(".page-title, h3")
        or soup.title
    )
    links = _links(root, url)
    pagination = []
    for anchor in root.select(".pagination a[href], .paging a[href], [rel=next][href], [rel=prev][href]"):
        target = safe_url(str(anchor.get("href", "")), url)
        if target and target not in pagination:
            pagination.append(target)
    return {
        "title": _text(heading),
        "url": safe_url(url, url),
        "text": _readable_text(root),
        "tables": [_table(table) for table in root.find_all("table") if table.find_parent("table") is None],
        "links": links,
        "attachments": _attachments(links),
        "pagination": pagination,
        "retrieved_at": datetime.now(SEOUL).isoformat(),
    }


def parse_courses(html: str, url: str) -> list[dict]:
    soup = _soup(html)
    courses: dict[str, dict] = {}
    for anchor in soup.select('a[href*="/course/view.php"]'):
        target = safe_url(str(anchor.get("href", "")), url)
        if not target:
            continue
        course_id = dict(parse_qsl(urlsplit(target).query)).get("id")
        if not course_id:
            continue
        card = (
            anchor.find_parent(
                class_=lambda value: value and ("course" in value.split() or "coursebox" in value.split())
            )
            or anchor
        )
        heading = card.select_one(".coursename, .course-title, .coursefullname") or anchor
        title_soup = BeautifulSoup(str(heading), "html.parser")
        for badge in title_soup.select(".badge"):
            badge.decompose()
        title = _text(title_soup)
        if not title:
            continue
        item = {"id": course_id, "title": title, "url": target}
        instructor = card.select_one(".prof, .teachers, .teacher, .instructor")
        if instructor and _text(instructor):
            item["instructor"] = _text(instructor)
        period = card.select_one(".badge-period")
        if period:
            item["status"] = _text(period)
            item["period"] = parse_dates(str(period.get("title", "")))
        code = re.search(r"\(([A-Za-z]{2,}[A-Za-z0-9]*-\d+)\)", title)
        if code:
            item["code"] = code.group(1)
        # Prefer rich course cards over breadcrumb/navigation copies.
        if course_id not in courses or len(item) > len(courses[course_id]):
            courses[course_id] = item
    return list(courses.values())


def _activity_type(node: Tag, target: str | None) -> str:
    match = re.search(r"/mod/([^/]+)/", target or "")
    if match:
        return match.group(1)
    for name in node.get("class", []):
        if name.startswith("modtype_"):
            return name.removeprefix("modtype_")
    return "unknown"


def _completion(node: Tag) -> dict | None:
    parts = []
    for mark in node.select(".autocompletion, .completion, .completion-info, [data-completion-state]"):
        parts.append(_text(mark))
        parts.extend(str(img.get("alt", "")) + " " + str(img.get("title", "")) for img in mark.select("img"))
        if mark.get("data-completion-state") is not None:
            parts.append("completion-state=" + str(mark["data-completion-state"]))
    raw = " ".join(part for part in parts if part).strip()
    if not raw:
        return None
    if re.search(r"미완료|완료하지|not complete|incomplete|completion-state=0", raw, re.I):
        status = "incomplete"
    elif re.search(r"완료|completed|completion-state=[12]", raw, re.I):
        status = "completed"
    else:
        status = "unknown"
    return {"status": status, "raw": raw}


def parse_activities(html: str, url: str) -> list[dict]:
    soup = _soup(html)
    root = _root(soup)
    containers = root.select("li.activity, div.activity, .timeline a.media, table.mod_index tbody tr")
    if not containers:
        containers = [
            anchor
            for anchor in root.select("a[href]")
            if re.search(r"/mod/[^/]+/view\.php", safe_url(str(anchor.get("href", "")), url) or "")
        ]
    result: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for node in containers:
        anchor = (
            node
            if node.name == "a"
            else node.select_one(".activityinstance a[href], a.aalink[href], a[href]")
        )
        target = safe_url(str(anchor.get("href", "")), url) if anchor else None
        query = dict(parse_qsl(urlsplit(target).query)) if target else {}
        activity_id = query.get("id") or str(node.get("id", "")).removeprefix("module-").removeprefix(
            "activity-"
        )
        activity_type = _activity_type(node, target)
        name = node.select_one(".instancename, .modname, .activityname")
        title = _text(name or anchor)
        if not title or not activity_id:
            continue
        identity = (activity_type, activity_id)
        if identity in seen:
            continue
        seen.add(identity)
        section = node.find_parent(class_=lambda value: value and "section" in value.split())
        section_heading = section.select_one(".sectionname, .section-title") if section else None
        raw = _text(node)
        display = node.select_one(".displayoptions")
        display_text = _text(display)
        late = display.select_one(".text-late") if display else None
        late_text = _text(late)
        regular_text = display_text.replace(late_text, "") if late_text else display_text
        display_dates = parse_dates(regular_text)
        item = {
            "id": activity_id,
            "type": activity_type,
            "title": title,
            "url": target,
            "source_url": safe_url(url, url),
            "section": _text(section_heading) or None,
            "raw_text": raw,
            "dates": parse_dates(raw),
            "display_dates": display_dates,
            "late_dates": parse_dates(late_text),
            "restricted": bool(node.select_one(".isrestricted")),
        }
        if node.name == "tr":
            cells = node.find_all("td", recursive=False)
            if cells and cells[0].find("a") is None:
                item["section"] = _text(cells[0]) or None
        if len(display_dates) >= 2:
            item["opens_at"] = display_dates[0]
            item["due_at"] = display_dates[1]
        if item["late_dates"]:
            item["late_due_at"] = item["late_dates"][0]
        availability = node.select_one(".availabilityinfo")
        if availability:
            item["availability"] = {"text": _text(availability), "dates": parse_dates(_text(availability))}
        timeline_date = node.select_one(".date[title]")
        if timeline_date:
            exact = parse_dates(str(timeline_date["title"]))
            if exact:
                item["due_at"] = exact[0]
                item["dates"] = exact
        course_title = node.select_one(".coursename")
        if course_title:
            item["course_title"] = _text(course_title)
        completion = _completion(node)
        if completion:
            item["completion"] = completion
        result.append(item)
    return result


_FIELD_ALIASES = {
    "submission_status": ("제출 여부", "제출 상태", "submission status"),
    "grading_status": ("채점 상황", "채점 상태", "grading status"),
    "due_date": ("종료 일시", "제출 기한", "마감 일시", "due date"),
    "cutoff_date": ("최종 제출 기한", "차단 일시", "cut-off date", "cutoff date"),
    "time_remaining": ("마감까지 남은 기한", "남은 시간", "time remaining"),
    "last_modified": ("최종 수정 일시", "마지막 수정", "last modified"),
}


def parse_assignment(html: str, url: str) -> dict:
    page = parse_page(html, url)
    soup = _soup(html)
    root = _root(soup)
    intro = root.select_one("#intro, .activity-description, .assignment-description")
    fields: dict[str, str] = {}
    for table in page["tables"]:
        for row in table["rows"]:
            if len(row) == 2 and row[0]:
                fields[row[0]] = row[1]
    page.update({"description": _readable_text(intro), "fields": fields})
    for canonical, aliases in _FIELD_ALIASES.items():
        value = next(
            (value for label, value in fields.items() if label.lower().strip().rstrip(":") in aliases), None
        )
        page[canonical] = value
        if canonical in {"due_date", "cutoff_date", "last_modified"}:
            page[canonical + "_parsed"] = parse_dates(value or "")
    page["due_at"] = next(iter(page["due_date_parsed"]), None)
    page["cutoff_at"] = next(iter(page["cutoff_date_parsed"]), None)
    status = page["submission_status"] or ""
    if re.search(r"제출 안|미제출|no submission|not submitted|nothing.+submitted", status, re.I):
        page["submitted"] = False
    elif re.search(r"제출 완료|제출함|제출 완료됨|submitted for grading", status, re.I):
        page["submitted"] = True
    else:
        page["submitted"] = None
    return page


def parse_announcements(html: str, url: str) -> dict:
    """Preserve a board/article page and identify actual discussion links."""
    page = parse_page(html, url)
    page["posts"] = [
        link
        for link in page["links"]
        if re.search(r"/mod/(?:forum/discuss|ubboard/(?:article|view))\.php", link["url"])
        and (
            set(dict(parse_qsl(urlsplit(link["url"]).query))) & {"d", "bwid"}
            or "/article.php" in urlsplit(link["url"]).path
        )
    ]
    soup = _soup(html)
    root = _root(soup)
    by_url = {post["url"]: post for post in page["posts"]}
    for row in root.select(".table-ubboard-list tbody tr, .forumheaderlist tbody tr"):
        for link in _links(row, url):
            if link["url"] not in by_url:
                continue
            post = by_url[link["url"]]
            post["id"] = dict(parse_qsl(urlsplit(link["url"]).query)).get("bwid") or dict(
                parse_qsl(urlsplit(link["url"]).query)
            ).get("d")
            post["author"] = _text(row.select_one(".t-writer, .author")) or None
            stamp = row.select_one(".t-date [title], .t-date, .lastpost")
            raw_date = str(stamp.get("title", "")) or _text(stamp) if stamp else ""
            post["dates"] = parse_dates(raw_date)
    article = root.select_one(".article-content, .posting.fullpost, .post-content")
    if article:
        page["article"] = {
            "title": _text(root.select_one(".article-subject, .subject")),
            "text": _readable_text(article),
            "url": safe_url(url, url),
            "metadata": _text(root.select_one(".article-info, .author")),
            "attachments": _attachments(_links(article, url)),
        }
        page["article"]["dates"] = parse_dates(page["article"]["metadata"])
        if page["article"]["title"]:
            page["title"] = page["article"]["title"]
    page["dates"] = parse_dates(page["text"])
    return page


_ATTENDANCE_COLUMNS = {
    "출석": "present",
    "지각": "late",
    "조퇴": "early_leave",
    "결석": "absent",
    "미처리": "unprocessed",
    "present": "present",
    "late": "late",
    "absent": "absent",
}


def _attendance_status(headers: list[str], row: list[str]) -> str:
    recorded = []
    for header, cell in zip(headers, row):
        label = header.strip().lower()
        value = cell.strip()
        if label in _ATTENDANCE_COLUMNS and value.upper() in {"O", "○", "●", "✓", "✔", "1"}:
            recorded.append(_ATTENDANCE_COLUMNS[label])
        elif "출결상태" in label or "attendance status" in label:
            status = _ATTENDANCE_COLUMNS.get(value.lower())
            if status:
                recorded.append(status)
    unique = set(recorded)
    if len(unique) == 1:
        return unique.pop()
    return "conflicting" if unique else "unknown"


def parse_attendance(html: str, url: str, *, as_of: date | datetime | None = None) -> dict:
    """Extract evidence and exclude future/default marks from attendance counts.

    ``as_of`` enables deterministic tests. Rows without explicit full dates are
    preserved but excluded from summary counts: a week number is not a date.
    """
    page = parse_page(html, url)
    if as_of is None:
        today = datetime.now(SEOUL).date()
    elif isinstance(as_of, datetime):
        today = as_of.astimezone(SEOUL).date() if as_of.tzinfo else as_of.date()
    else:
        today = as_of
    records = []
    counts = {"present": 0, "late": 0, "early_leave": 0, "absent": 0}
    excluded = {"future": 0, "date_unknown": 0, "unprocessed": 0, "unknown": 0, "conflicting": 0}
    for table_index, table in enumerate(page["tables"]):
        for row in table["rows"]:
            row_dates = parse_dates(" ".join(row))
            day = date.fromisoformat(row_dates[0]["iso"][:10]) if row_dates else None
            recorded = _attendance_status(table["headers"], row)
            future = day > today if day else None
            status = "scheduled" if future else recorded
            countable = day is not None and not future and status in counts
            reason = (
                "future" if future else "date_unknown" if day is None else status if not countable else None
            )
            record = {
                "table_index": table_index,
                "headers": table["headers"],
                "cells": row,
                "dates": row_dates,
                "recorded_status": recorded,
                "status": status,
                "is_future": future,
                "countable": countable,
                "excluded_reason": reason,
            }
            if countable:
                counts[status] += 1
            elif reason in excluded:
                excluded[reason] += 1
            records.append(record)
    page["records"] = records
    page["summary"] = {
        "as_of": today.isoformat(),
        "counts": counts,
        "excluded": excluded,
        "counted_records": sum(counts.values()),
        "note": "Future rows and rows without a full date are excluded. Counts describe recorded LMS marks, not a final grade.",
    }
    return page
