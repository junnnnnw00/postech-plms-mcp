import pytest

from plms_mcp.policies import compare_rules, extract_rules


def test_korean_absence_rule_carries_exact_evidence():
    result = extract_rules(
        "강의 개요\n결석 3회 이상인 경우 F학점을 부여한다.", "https://plms.example/syllabus"
    )
    rule = result["rules"][0]
    assert rule["conditions"][0] == {
        "metric": "absence_count",
        "operator": ">=",
        "threshold": 3.0,
        "consequence": "failing_grade_risk",
    }
    assert rule["evidence"]["locator"] == "line:2"
    assert rule["evidence"]["source_url"] == "https://plms.example/syllabus"


@pytest.mark.parametrize(
    "text,count,status",
    [
        ("결석 3회 이상인 경우 F학점.", 3, "risk"),
        ("결석 3회 초과인 경우 F학점.", 3, "below_stated_threshold"),
        ("More than 3 absences results in failure.", 4, "risk"),
        ("At least 3 absences results in failure.", 2, "below_stated_threshold"),
    ],
)
def test_threshold_boundary_is_explicit(text, count, status):
    result = compare_rules(
        text,
        [{"kind": "attendance", "absence_count": count, "source_url": "https://plms.example/attendance"}],
    )
    assert result["comparisons"][0]["status"] == status
    assert result["official_determination"] is False


def test_ratio_needs_full_course_denominator():
    syllabus = "결석이 수업일수의 1/4 이상이면 F학점 처리한다."
    unknown = compare_rules(syllabus, [{"absence_count": 4, "elapsed_sessions": 8}])
    assert unknown["comparisons"][0]["status"] == "unknown"
    below = compare_rules(syllabus, [{"absence_count": 4, "total_scheduled_sessions": 32}])
    assert below["comparisons"][0]["status"] == "below_stated_threshold"
    risk = compare_rules(syllabus, [{"absence_count": 8, "total_scheduled_sessions": 32}])
    assert risk["comparisons"][0]["status"] == "risk"


@pytest.mark.parametrize(
    "text",
    [
        "결석 3회 이상이어도 F학점을 부여하지 않는다.",
        "결석 3회 이상이면 F학점이나 공결은 제외한다.",
        "Attendance is expected.",
        "출석은 성적에 반영한다.",
    ],
)
def test_no_invented_failure_or_unqualified_exceptions(text):
    extracted = extract_rules(text)
    assert extracted["rules"]
    assert not extracted["rules"][0]["conditions"]
    compared = compare_rules(text, [{"absence_count": 99}])
    assert not compared["comparisons"]


def test_late_conversion_does_not_guess_rounding_or_double_count():
    compared = compare_rules(
        "지각 3회는 결석 1회로 간주한다.", [{"kind": "attendance", "late_count": 4, "absence_count": 1}]
    )
    comparison = compared["comparisons"][0]
    assert comparison["status"] == "unknown"
    assert comparison["calculation"]["proportional_equivalent"] == pytest.approx(4 / 3)


def test_deadlines_normalize_korea_and_compare_same_assignment():
    result = compare_rules(
        "HW1 제출 기한: 2026-09-28 23:59",
        [{"kind": "assignment", "title": "HW1", "due_at": "2026-09-28T14:59:00Z"}],
    )
    assert result["comparisons"][0]["status"] == "match"
    assert result["comparisons"][0]["calculation"]["syllabus_due_at"] == "2026-09-28T23:59:00+09:00"


def test_deadline_difference_is_conflict_not_chosen_truth():
    result = compare_rules(
        "HW1 due 2026/09/28 오후 11시 30분", [{"title": "HW1", "due_at": "2026-09-29T23:30:00+09:00"}]
    )
    comparison = result["comparisons"][0]
    assert comparison["status"] == "conflict"
    assert comparison["calculation"]["difference_seconds"] == 86400


@pytest.mark.parametrize("date_text", ["9/28 23:59", "2026-09-28"])
def test_missing_year_or_time_remains_unknown(date_text):
    result = compare_rules(f"HW1 due {date_text}", [{"title": "HW1", "due_at": "2026-09-28T23:59:00+09:00"}])
    assert result["comparisons"][0]["status"] == "unknown"


def test_unrelated_or_prefix_assignment_does_not_match():
    result = compare_rules(
        "HW10 due 2026-09-28 23:59", [{"title": "HW1", "due_at": "2026-09-28T23:59:00+09:00"}]
    )
    assert not result["comparisons"]


def test_multiple_dates_are_not_misinterpreted_as_deadline():
    result = compare_rules(
        "HW1 제출 기간: 2026-09-01 12:00 ~ 2026-09-28 23:59",
        [{"title": "HW1", "due_at": "2026-09-28T23:59:00+09:00"}],
    )
    assert result["comparisons"][0]["status"] == "unknown"


def test_conflicting_thresholds_are_exposed():
    result = compare_rules("결석 3회 이상이면 F학점.\n결석 4회 이상이면 F학점.", [{"absence_count": 3}])
    assert any("Multiple absence_count" in warning for warning in result["warnings"])
    assert all(any("Multiple syllabus" in text for text in c["uncertainty"]) for c in result["comparisons"])


def test_hours_cannot_be_compared_to_session_count():
    result = compare_rules("결석 6시간 이상이면 F학점.", [{"kind": "attendance", "absence_count": 6}])
    assert result["comparisons"][0]["status"] == "unknown"


def test_non_numeric_and_negative_observations_are_unknown():
    for value in (True, -1, "nan", "unavailable"):
        result = compare_rules("결석 3회 이상이면 F학점.", [{"absence_count": value}])
        assert result["comparisons"][0]["status"] == "unknown"


def test_syllabus_header_is_context_without_altering_evidence():
    result = extract_rules("Attendance policy:\nMore than 3 absences results in failure.")
    rule = result["rules"][0]
    assert rule["context_header"] == "Attendance policy:"
    assert rule["evidence"]["locator"] == "line:2"
    assert rule["text"] == "More than 3 absences results in failure."


def test_short_non_numeric_policy_not_discarded_as_header():
    result = extract_rules("지연 제출은 허용하지 않습니다.")
    assert len(result["rules"]) == 1
    assert "late_submission" in result["rules"][0]["categories"]


def test_unrelated_exam_failure_is_not_an_attendance_consequence():
    result = extract_rules("결석 3회 이상 학생은 상담 대상이다. 시험 낙제 시 F학점을 부여한다.")
    assert result["rules"]
    assert not result["rules"][0]["conditions"]


@pytest.mark.parametrize("count,status", [(1, "unknown"), (3, "risk")])
def test_incomplete_attendance_is_lower_bound_not_clearance(count, status):
    result = compare_rules(
        "결석 3회 이상이면 F학점.",
        [{"kind": "attendance", "absence_count": count, "records_complete": False}],
    )
    comparison = result["comparisons"][0]
    assert comparison["status"] == status
    assert any("lower bound" in message for message in comparison["uncertainty"])
