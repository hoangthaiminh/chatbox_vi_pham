import pytest

from violations.models import Candidate, Incident
from violations.realtime import build_candidate_stats
from violations.services import sync_incident_references

pytestmark = pytest.mark.django_db


def _create_incident(primary_sbd, text, incident_kind):
    incident = Incident()
    sync_incident_references(
        incident=incident,
        primary_sbd=primary_sbd,
        violation_text=text,
        incident_kind=incident_kind,
    )
    return incident


def test_conversion_rule_and_out_flag_for_known_candidates():
    Candidate.objects.create(
        sbd="TS1001",
        full_name="Alpha",
        school="S1",
        supervisor_teacher="T1",
        exam_room="R1",
    )
    Candidate.objects.create(
        sbd="TS1002",
        full_name="Beta",
        school="S1",
        supervisor_teacher="T2",
        exam_room="R1",
    )

    _create_incident("TS1001", "violation #1", Incident.KIND_VIOLATION)
    _create_incident("TS1001", "reminder #1", Incident.KIND_REMINDER)
    _create_incident("TS1001", "reminder #2", Incident.KIND_REMINDER)

    _create_incident("TS1002", "violation #1", Incident.KIND_VIOLATION)
    _create_incident("TS1002", "violation #2", Incident.KIND_VIOLATION)

    candidate_stats, _ = build_candidate_stats()
    by_sbd = {row["sbd"]: row for row in candidate_stats}

    assert by_sbd["TS1001"]["violation_count"] == 1
    assert by_sbd["TS1001"]["reminder_count"] == 2
    assert by_sbd["TS1001"]["effective_violations"] == 2
    assert by_sbd["TS1001"]["is_out"] is True

    assert by_sbd["TS1002"]["violation_count"] == 2
    assert by_sbd["TS1002"]["reminder_count"] == 0
    assert by_sbd["TS1002"]["effective_violations"] == 2
    assert by_sbd["TS1002"]["is_out"] is True


def test_conversion_rule_and_out_flag_for_unmatched_sbd():
    _create_incident("TS9991", "reminder #1", Incident.KIND_REMINDER)
    _create_incident("TS9991", "reminder #2", Incident.KIND_REMINDER)
    _create_incident("TS9991", "reminder #3", Incident.KIND_REMINDER)
    _create_incident("TS9991", "reminder #4", Incident.KIND_REMINDER)

    _, unknown_stats = build_candidate_stats()
    by_sbd = {row["normalized_sbd"]: row for row in unknown_stats}

    assert by_sbd["TS9991"]["violation_count"] == 0
    assert by_sbd["TS9991"]["reminder_count"] == 4
    assert by_sbd["TS9991"]["effective_violations"] == 2
    assert by_sbd["TS9991"]["is_out"] is True
