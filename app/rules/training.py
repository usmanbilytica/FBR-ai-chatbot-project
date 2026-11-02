from __future__ import annotations

from datetime import date, datetime
from typing import Any

from app.db.hrms import hrms_execute
from app.rules.types import RuleResult, Severity


def _age_on(dob: Any, on: date) -> int | None:
    if dob is None:
        return None
    if isinstance(dob, datetime):
        d = dob.date()
    elif isinstance(dob, date):
        d = dob
    else:
        try:
            d = datetime.fromisoformat(str(dob)[:10]).date()
        except Exception:
            return None
    years = on.year - d.year - ((on.month, on.day) < (d.month, d.day))
    return years


def eligibility_training_nomination(
    officer: dict[str, Any],
    course: dict[str, Any],
    *,
    application_date: date | None = None,
    prior_class3_certs: list[str] | None = None,
    already_completed: bool = False,
    already_rejected: bool = False,
    already_nominated: bool = False,
) -> RuleResult:
    reasons: list[str] = []
    status = Severity.PASS
    application_date = application_date or date.today()
    course_name = str(course.get("CourseName") or course.get("name") or "")
    if course_name.strip().upper() == "LMS" or course_name.strip().upper().startswith("LMS "):
        return RuleResult(
            Severity.BLOCK,
            reasons=["Course named LMS is not an offered training"],
            citations=["rules:training"],
        )

    if already_completed or already_rejected or already_nominated:
        return RuleResult(
            Severity.EXCLUDE,
            reasons=["Already completed / rejected / nominated → do not nominate"],
            citations=["rules:training"],
        )

    age = _age_on(officer.get("DOB"), application_date)
    age_min = course.get("age_min")
    age_max = course.get("age_max")
    if age is None and (age_min is not None or age_max is not None):
        status = Severity.MISSING
        reasons.append("DOB missing for age-band check")
    elif age is not None:
        if age_min is not None and age < int(age_min):
            status = Severity.FAIL
            reasons.append(f"Age {age} < min {age_min}")
        if age_max is not None and age > int(age_max):
            status = Severity.FAIL
            reasons.append(f"Age {age} > max {age_max}")
        if age_min is None and age_max is None:
            reasons.append("No age band on course")

    # Include lists
    og = officer.get("OrgLevel2")
    allow_org = course.get("EligibilityOrgLevel2Ids") or course.get("allowed_org_level2")
    if allow_org:
        allowed = {int(x) for x in str(allow_org).replace(";", ",").split(",") if str(x).strip().isdigit()}
        if og is not None and int(og) not in allowed:
            status = Severity.FAIL
            reasons.append(f"OrgLevel2 {og} not in course eligibility list")

    grade_ok = course.get("allowed_grades")
    if grade_ok and officer.get("EmployeeGradeID") not in set(grade_ok):
        status = Severity.FAIL
        reasons.append("Grade not in include list")

    # Dual eligibility: course↔employee table
    course_id = course.get("ID") or course.get("CourseID")
    emp_id = officer.get("ID") or officer.get("employee_id")
    if course_id is not None and emp_id is not None:
        rows = hrms_execute(
            """
            SELECT TOP 1 ID FROM dbo.hr_training_course_eligibility_dt
            WHERE CourseID = :cid AND EmployeeID = :eid
            """,
            {"cid": course_id, "eid": emp_id},
        )
        # Table may be sparse — if any rows exist for course, require membership
        any_for_course = hrms_execute(
            "SELECT TOP 1 ID FROM dbo.hr_training_course_eligibility_dt WHERE CourseID = :cid",
            {"cid": course_id},
        )
        if any_for_course and not rows:
            status = Severity.FAIL
            reasons.append("Officer not on hr_training_course_eligibility_dt for this course")
        elif rows:
            reasons.append("Course↔employee eligibility row present")
        else:
            reasons.append("No per-employee eligibility rows for course; org/grade gates apply")

    # Prior certification courses must appear on Class-3 docs
    required_prior = course.get("required_prior_certs") or []
    if required_prior:
        have = {c.lower() for c in (prior_class3_certs or [])}
        missing = [c for c in required_prior if c.lower() not in have]
        if missing:
            status = Severity.FAIL
            reasons.append(f"Missing Class-3 prior certs: {missing}")
        else:
            reasons.append("Prior Class-3 certifications present")

    return RuleResult(
        status=status,
        reasons=reasons,
        citations=["rules:training"],
        data={"age": age, "course": course_name},
    )
