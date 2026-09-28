"""Evidence-first syllabus rules and conservative comparisons.

This module identifies explicit Korean/English statements, not institutional policy.
Unsupported phrasing remains a quoted rule requiring human interpretation. A rule
trigger is a warning to confirm with the instructor, never a final grade decision.
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

KOREA = ZoneInfo("Asia/Seoul")
MAX_SYLLABUS_CHARS = 2_000_000
MAX_RULES = 500
MAX_OBSERVATIONS = 2000
MAX_COMPARISONS = 2000

_CATEGORIES = {
    "attendance": r"출석|결석|지각|조퇴|공결|attendance|absen[ct]|tardy|tardiness",
    "deadline": r"마감|제출\s*(?:기한|기간|일시|일자)|기한|due\b|deadline|submit.{0,20}by\b",
    "late_submission": r"지연\s*제출|늦은\s*제출|늦게\s*제출|기한\s*(?:후|초과)|late\s+(?:submission|work|assignment)|penalt",
    "grading": r"평가|성적|배점|반영\s*비율|grading|grade\b|weight|\d\s*%",
    "exam": r"중간\s*고사|기말\s*고사|시험|exam|midterm|final\s+exam|quiz",
    "academic_integrity": r"표절|부정\s*행위|생성형|ChatGPT|plagiar|academic\s+integrity|cheat|generative\s+AI",
    "participation": r"참여|토론|발표|participat|discussion|presentation",
    "materials": r"교재|참고\s*문헌|준비물|textbook|reading|material",
    "accommodation": r"장애|편의\s*지원|특별\s*배려|accommodat|disabilit",
    "contact": r"면담|연락|office\s*hours|e-?mail|이메일",
}
_FAIL = re.compile(
    r"(?<![A-Za-z])F(?:학점|\s*학점|\b)|낙제|학점\s*(?:미부여|부여\s*불가)|fail(?:ure|ing)?", re.IGNORECASE
)
_ABSENCE = re.compile(r"결석|absen[ct]", re.IGNORECASE)


def _evidence(line: str, line_number: int, source_url: str) -> dict[str, Any]:
    return {"excerpt": line[:1800], "locator": f"line:{line_number}", "source_url": source_url or None}


def _number(value: str) -> float:
    if "/" in value:
        numerator, denominator = value.split("/")
        return float(numerator) / float(denominator)
    return float(value)


def _comparison(word: str) -> str | None:
    word = word.lower().strip()
    if re.fullmatch(r"이상|at\s+least|or\s+more|>=|≥", word):
        return ">="
    if re.fullmatch(r"초과|more\s+than|over|>|greater\s+than", word):
        return ">"
    return None


def _absence_condition(text: str) -> dict[str, Any] | None:
    if not _ABSENCE.search(text) or not _FAIL.search(text):
        return None
    # An exception/negation cannot be converted safely into an unconditional rule.
    if re.search(r"예외|제외|공결|인정\s*결석|않|아니|except|unless|excused|\bnot\b", text, re.IGNORECASE):
        return None
    # A paragraph may mention an attendance warning and an unrelated failing
    # exam grade. Never connect those statements merely because PDF extraction
    # placed them on one line.
    sentences = re.split(r"(?<=[.!?。])\s+|[;；]", text)
    candidates = [sentence for sentence in sentences if _ABSENCE.search(sentence) and _FAIL.search(sentence)]
    if len(candidates) != 1:
        return None
    text = candidates[0]
    patterns = [
        r"(?:결석|absences?).{0,24}?(\d+(?:\.\d+)?(?:/\d+)?)\s*(회|번|시간|%|percent|sessions?|classes?|hours?)?\s*(이상|초과|or\s+more)",
        r"(\d+(?:\.\d+)?(?:/\d+)?)\s*(회|번|시간|%|percent|sessions?|classes?|hours?)?\s*(이상|초과|or\s+more)\s*(?:의\s*)?(?:결석|absences?)",
    ]
    found = None
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            found = (match[1], match[2], match[3])
            break
    if not found:
        match = re.search(
            r"(at\s+least|more\s+than|over|>=|≥|>)\s*(\d+(?:\.\d+)?(?:/\d+)?)\s*(%|percent|sessions?|classes?|hours?)?.{0,20}?(?:absen[ct]|of\s+(?:all\s+)?classes)",
            text,
            re.IGNORECASE,
        )
        if match:
            found = (match[2], match[3], match[1])
    if not found:
        # e.g. "absence for more than 25% of classes results in failure"
        match = re.search(
            r"absen[ct].{0,20}?(at\s+least|more\s+than|over|>=|≥|>)\s*(\d+(?:\.\d+)?(?:/\d+)?)\s*(%|percent|sessions?|classes?|hours?)?",
            text,
            re.IGNORECASE,
        )
        if match:
            found = (match[2], match[3], match[1])
    if not found:
        return None
    raw_value, unit, comparator = found
    value = _number(raw_value)
    if unit in {"%", "percent"}:
        value /= 100
        metric = "absence_fraction"
    elif "/" in raw_value:
        metric = "absence_fraction"
    elif unit and re.fullmatch(r"시간|hours?", unit, re.IGNORECASE):
        metric = "absence_hours"
    elif unit or (value >= 1 and re.search(r"absences?", text, re.IGNORECASE)):
        metric = "absence_count"
    else:
        return None
    if value < 0 or (metric == "absence_fraction" and value > 1):
        return None
    return {
        "metric": metric,
        "operator": _comparison(comparator),
        "threshold": value,
        "consequence": "failing_grade_risk",
    }


def _late_conversion(text: str) -> dict[str, Any] | None:
    if not re.search(r"지각|tard|late\s+(?:arrival|attendance)", text, re.IGNORECASE) or not _ABSENCE.search(
        text
    ):
        return None
    if re.search(r"예외|제외|않|not|except", text, re.IGNORECASE):
        return None
    patterns = [
        r"지각\s*(\d+)\s*(?:회|번).{0,18}?결석\s*(\d+)\s*(?:회|번)",
        r"(\d+)\s*(?:tardies|late\s+arrivals?).{0,25}?(\d+)\s*absences?",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match and int(match[1]) > 0 and int(match[2]) > 0:
            return {
                "metric": "late_to_absence",
                "late_count": int(match[1]),
                "absence_count": int(match[2]),
                "aggregation": "unspecified",
            }
    return None


def _dates(text: str) -> list[dict[str, Any]]:
    """Extract only explicit years; missing year/time is exposed, never guessed."""
    dates: list[dict[str, Any]] = []
    pattern = r"(?<!\d)(?:(\d{4})\s*[년./-]\s*)?(\d{1,2})\s*[월./-]\s*(\d{1,2})(?:\s*일)?(?!\d)(?:\s*(?:\([^)]{1,8}\)|[월화수목금토일]요일)?\s*(?:(오전|오후)\s*)?(\d{1,2})\s*[:시]\s*(\d{2})?\s*분?)?"
    for match in re.finditer(pattern, text):
        year, month, day, meridiem, hour, minute = match.groups()
        # Fractions (1/4 of classes) are not dates. Month/day without a year is
        # accepted only in lines already classified as deadline/exam information.
        try:
            y, m, d = int(year) if year else None, int(month), int(day)
            date(y or 2000, m, d)
            h = int(hour) if hour is not None else None
            if h is not None and meridiem:
                if not 1 <= h <= 12:
                    continue
                h = h % 12 + (12 if meridiem == "오후" else 0)
            minutes = int(minute or 0)
            if h is not None:
                time(h, minutes)
            timestamp = (
                datetime(y, m, d, h, minutes, tzinfo=KOREA).isoformat() if y and h is not None else None
            )
            dates.append(
                {
                    "raw": match[0].strip(),
                    "date": date(y, m, d).isoformat() if y else None,
                    "month": m,
                    "day": d,
                    "year": y,
                    "time": f"{h:02d}:{minutes:02d}" if h is not None else None,
                    "datetime": timestamp,
                    "timezone": "Asia/Seoul",
                    "precision": "datetime" if timestamp else ("date" if y else "month_day"),
                    "uncertainty": [
                        message
                        for condition, message in (
                            (not y, "Year not stated in this rule."),
                            (h is None, "Time of day is not stated; no end-of-day deadline assumed."),
                        )
                        if condition
                    ],
                }
            )
        except ValueError:
            continue
    return dates


def extract_rules(text: str, source_url: str = "") -> dict[str, Any]:
    """Return quoted candidate rules, explicitly parsed conditions, and uncertainty."""
    if not isinstance(text, str):
        raise TypeError("Syllabus text must be a string")
    truncated = len(text) > MAX_SYLLABUS_CHARS
    text = text[:MAX_SYLLABUS_CHARS]
    rules = []
    warnings = [
        "Rules are extracted from the supplied syllabus only; notices, revised syllabi and instructor exceptions may override them."
    ]
    previous_header = ""
    for line_number, raw_line in enumerate(text.splitlines(), 1):
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line:
            continue
        categories = [
            category for category, pattern in _CATEGORIES.items() if re.search(pattern, line, re.IGNORECASE)
        ]
        # A short standalone label helps identify the next bullet but is never
        # included in the excerpt as though it appeared on the same source line.
        is_header = re.fullmatch(
            r"(?:출석|출결|결석|성적|평가|과제|시험|교재|참고문헌|면담|attendance|absence|grading|assignments?|exams?|materials?|office hours)(?:\s*(?:규정|정책|policy|policies))?\s*:?",
            line,
            re.IGNORECASE,
        )
        if is_header:
            previous_header = line
            continue
        if not categories and previous_header:
            categories = [
                category
                for category, pattern in _CATEGORIES.items()
                if re.search(pattern, previous_header, re.IGNORECASE)
            ]
        if not categories:
            previous_header = ""
            continue
        context = f"{previous_header} {line}" if previous_header else line
        conditions = []
        for parsed in (_absence_condition(context), _late_conversion(context)):
            if parsed:
                conditions.append(parsed)
        dates = _dates(line) if set(categories) & {"deadline", "exam"} else []
        uncertainty = []
        if not conditions and "attendance" in categories:
            uncertainty.append(
                "Attendance statement preserved as evidence; no numeric consequence safely parsed."
            )
        if re.search(
            r"예외|제외|공결|인정\s*결석|except|unless|excused|subject\s+to", context, re.IGNORECASE
        ):
            uncertainty.append(
                "Rule contains an exception or qualification that requires instructor confirmation."
            )
        if dates and len(dates) > 1:
            uncertainty.append("Multiple dates occur in this statement; their roles require interpretation.")
        rule = {
            "id": f"rule-{line_number}",
            "categories": categories,
            "text": line,
            "evidence": _evidence(line, line_number, source_url),
            "conditions": conditions,
            "dates": dates,
            "confidence": "explicit_numeric" if conditions else "quoted_only",
            "uncertainty": uncertainty,
        }
        if previous_header:
            rule["context_header"] = previous_header
        rules.append(rule)
        if len(rules) >= MAX_RULES:
            truncated = True
            break
    if truncated:
        warnings.append("Syllabus/rule extraction limit reached; this is a partial analysis.")
    signatures: dict[str, set[tuple]] = {}
    for rule in rules:
        for condition in rule["conditions"]:
            if condition["metric"] != "late_to_absence":
                signatures.setdefault(condition["metric"], set()).add(
                    (condition["operator"], condition["threshold"])
                )
    for metric, values in signatures.items():
        if len(values) > 1:
            warnings.append(
                f"Multiple {metric} thresholds were found; course scope or conflicting versions must be checked."
            )
    return {
        "source_url": source_url or None,
        "timezone": "Asia/Seoul",
        "rules": rules,
        "warnings": warnings,
        "truncated": truncated,
        "requires_confirmation": True,
    }


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else None
    except (TypeError, ValueError):
        return None


def _timestamp(value: Any) -> tuple[datetime | None, list[str]]:
    if not isinstance(value, str) or not value.strip():
        return None, []
    # Date-only observations must not become invented midnight deadlines.
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
        return None, ["Observation specifies a date only, without a time."]
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None, ["Observation timestamp is not a supported ISO 8601 datetime."]
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=KOREA), ["Observation has no timezone; interpreted as Asia/Seoul."]
    return parsed.astimezone(KOREA), []


def _observation_evidence(observation: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_url": observation.get("source_url") or observation.get("url"),
        "observed_at": observation.get("observed_at"),
        "label": observation.get("title")
        or observation.get("name")
        or observation.get("kind")
        or observation.get("type"),
        "data": {k: v for k, v in observation.items() if k not in {"source_url", "url", "observed_at"}},
    }


def _attendance_check(
    rule: dict[str, Any], condition: dict[str, Any], observation: dict[str, Any]
) -> dict[str, Any]:
    metric = condition["metric"]
    result: dict[str, Any] = {
        "rule_id": rule["id"],
        "category": "attendance",
        "status": "unknown",
        "syllabus_evidence": rule["evidence"],
        "observation_evidence": _observation_evidence(observation),
        "uncertainty": list(rule["uncertainty"]),
    }
    if metric == "late_to_absence":
        count = _finite_number(observation.get("late_count", observation.get("tardy_count")))
        result["message"] = (
            "The syllabus states a late-arrival conversion; rounding and whether the LMS absence count already includes it require confirmation."
        )
        if count is not None:
            result["calculation"] = {
                "late_count": count,
                "stated_ratio": f"{condition['late_count']} late arrivals : {condition['absence_count']} absences",
                "proportional_equivalent": count / condition["late_count"] * condition["absence_count"],
            }
        return result
    if metric == "absence_hours":
        value = _finite_number(observation.get("absence_hours"))
    else:
        value = _finite_number(
            observation.get("absence_count", observation.get("absences", observation.get("missed_sessions")))
        )
        if metric == "absence_fraction":
            total = _finite_number(
                observation.get("total_scheduled_sessions", observation.get("total_class_sessions"))
            )
            if total is None or total <= 0:
                value = None
                result["uncertainty"].append(
                    "Full-course scheduled session count is required; elapsed classes are not a valid denominator."
                )
            elif value is not None:
                value /= total
    if value is None:
        result["message"] = "Required attendance counts or units are unavailable."
        return result
    if metric == "absence_fraction" and value > 1:
        result["message"] = "Absence count exceeds scheduled class count; the observation is inconsistent."
        return result
    operator, threshold = condition["operator"], condition["threshold"]
    triggered = value >= threshold if operator == ">=" else value > threshold
    result["status"] = "risk" if triggered else "below_stated_threshold"
    result["calculation"] = {
        "metric": metric,
        "observed": value,
        "operator": operator,
        "threshold": threshold,
        "threshold_triggered": triggered,
    }
    result["message"] = (
        "Observed absences meet the syllabus's stated failing-grade condition; verify records, exceptions and instructor interpretation."
        if triggered
        else "Observed absences are below this stated threshold as of this observation."
    )
    if observation.get("records_complete") is False:
        result["uncertainty"].append(
            "Attendance records are incomplete; the reported absence count may be only a lower bound while unknown or unprocessed entries remain."
        )
        if not triggered:
            result["status"] = "unknown"
            result["message"] = (
                "Known absences have not reached the stated threshold, but incomplete attendance records prevent a below-threshold conclusion."
            )
    result["uncertainty"].append(
        "This comparison is not an official grade or attendance determination; approved absences, converted tardiness and incomplete records may change the outcome."
    )
    return result


def _same_item(rule: dict[str, Any], observation: dict[str, Any]) -> bool:
    if observation.get("rule_id") == rule["id"]:
        return True
    label = str(observation.get("title") or observation.get("name") or "").strip().casefold()
    return (
        len(label) >= 3
        and re.search(r"(?<![A-Za-z0-9])" + re.escape(label) + r"(?![A-Za-z0-9])", rule["text"].casefold())
        is not None
    )


def _deadline_check(rule: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "rule_id": rule["id"],
        "category": "deadline",
        "status": "unknown",
        "syllabus_evidence": rule["evidence"],
        "observation_evidence": _observation_evidence(observation),
        "uncertainty": list(rule["uncertainty"]),
    }
    if len(rule["dates"]) != 1:
        result["message"] = "A single unambiguous syllabus deadline is required for an automatic comparison."
        return result
    stated = rule["dates"][0]
    result["uncertainty"].extend(stated["uncertainty"])
    observed, warnings = _timestamp(observation.get("due_at", observation.get("deadline")))
    result["uncertainty"].extend(warnings)
    if observed is None or stated["datetime"] is None:
        result["message"] = (
            "An explicit year, time and matching LMS deadline are required for precise comparison."
        )
        return result
    syllabus_due = datetime.fromisoformat(stated["datetime"])
    delta = (observed - syllabus_due).total_seconds()
    result["status"] = "match" if delta == 0 else "conflict"
    result["calculation"] = {
        "syllabus_due_at": syllabus_due.isoformat(),
        "observed_due_at": observed.isoformat(),
        "difference_seconds": delta,
    }
    result["message"] = (
        "LMS and syllabus deadlines match."
        if delta == 0
        else "LMS and syllabus deadlines differ. Check the latest announcement or instructor clarification before choosing a deadline."
    )
    return result


def compare_rules(syllabus_text: str, observations: list[dict], source_url: str = "") -> dict[str, Any]:
    """Compare explicit rules against source-bearing observations.

    Attendance observation example: {kind: 'attendance', absence_count: 3,
    total_scheduled_sessions: 28, late_count: 1, source_url: '...', observed_at: '...'}.
    Deadline: {kind: 'assignment', title: 'HW1', due_at: ISO8601, source_url: '...'}.
    An explicit rule_id can map titles which differ; unrelated assignments are
    never matched by position. Dates without year/time cannot produce a conflict.
    """
    if not isinstance(observations, list):
        raise TypeError("Observations must be a list of dictionaries")
    extracted = extract_rules(syllabus_text, source_url)
    comparisons = []
    warnings = list(extracted["warnings"])
    if len(observations) > MAX_OBSERVATIONS:
        warnings.append("Observation limit reached; analysis is partial.")
    for index, observation in enumerate(observations[:MAX_OBSERVATIONS]):
        if len(comparisons) >= MAX_COMPARISONS:
            warnings.append("Comparison limit reached; analysis is partial.")
            break
        if not isinstance(observation, dict):
            warnings.append(f"Observation {index} was skipped because it is not an object.")
            continue
        kind = str(observation.get("kind", observation.get("type", ""))).lower()
        attendance = kind in {"attendance", "absence", "출석", "결석"} or any(
            k in observation
            for k in (
                "absence_count",
                "absence_hours",
                "absences",
                "missed_sessions",
                "late_count",
                "tardy_count",
            )
        )
        for rule in extracted["rules"]:
            if len(comparisons) >= MAX_COMPARISONS:
                break
            if observation.get("rule_id") and observation["rule_id"] != rule["id"]:
                continue
            if attendance:
                for condition in rule["conditions"]:
                    comparisons.append(_attendance_check(rule, condition, observation))
            if (
                ("due_at" in observation or "deadline" in observation)
                and "deadline" in rule["categories"]
                and _same_item(rule, observation)
            ):
                comparisons.append(_deadline_check(rule, observation))
    if not comparisons:
        warnings.append(
            "No observations could be matched to a safely parsed rule. Review the quoted rules; absence of a comparison does not imply compliance."
        )
    # Contradictory thresholds are not silently resolved by choosing one.
    if any(warning.startswith("Multiple ") for warning in warnings):
        for comparison in comparisons:
            if comparison["category"] == "attendance":
                comparison["uncertainty"].append(
                    "Multiple syllabus thresholds require checking scope/version before acting."
                )
    return {
        "source_url": source_url or None,
        "timezone": "Asia/Seoul",
        "rules": extracted["rules"],
        "comparisons": comparisons[:MAX_COMPARISONS],
        "warnings": warnings,
        "truncated": extracted["truncated"]
        or len(observations) > MAX_OBSERVATIONS
        or len(comparisons) >= MAX_COMPARISONS,
        "official_determination": False,
    }
