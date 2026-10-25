"""Conservative structured identity matching; never infer sample IDs from text."""
import re


def normalized_test(value):
    return re.sub(r"(?:结果|报告)+$", "", re.sub(r"\s+", "", value or "")).casefold()


def matches_report(loop, report):
    criteria = loop.matching_criteria or {}
    if (loop.patient_id, loop.encounter_id) != (report.patient_id, report.encounter_id):
        return False
    if not any(criteria.get(k) for k in ("specimen_id", "order_id", "report_id")):
        return False
    if normalized_test(criteria.get("test_name")) != normalized_test(report.test_name):
        return False
    return all(not criteria.get(key) or criteria[key] == getattr(report, key) for key in ("specimen_id", "order_id", "report_id"))
