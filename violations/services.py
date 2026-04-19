import re
from collections import OrderedDict

from django.db import transaction
from django.db.models.functions import Upper

from .models import Candidate, IncidentParticipant

SBD_PATTERN = re.compile(r"\b[Tt][Ss]\d{4}\b")


def normalize_sbd(value):
    return (value or "").upper().strip()


def extract_sbd_codes(text):
    normalized = OrderedDict()
    for code in SBD_PATTERN.findall(text or ""):
        normalized[normalize_sbd(code)] = True
    return list(normalized.keys())


@transaction.atomic
def sync_incident_references(incident, primary_sbd, violation_text):
    primary_sbd = normalize_sbd(primary_sbd)
    referenced_codes = extract_sbd_codes(violation_text)

    ordered_codes = [primary_sbd]
    for code in referenced_codes:
        if code != primary_sbd:
            ordered_codes.append(code)

    candidates = {
        candidate.normalized_sbd: candidate
        for candidate in Candidate.objects.annotate(
            normalized_sbd=Upper("sbd")
        ).filter(normalized_sbd__in=ordered_codes)
    }

    incident.reported_sbd = primary_sbd
    incident.reported_candidate = candidates.get(primary_sbd)
    incident.violation_text = violation_text.strip()
    incident.save()

    incident.participants.all().delete()
    participant_rows = []
    for index, sbd in enumerate(ordered_codes):
        participant_rows.append(
            IncidentParticipant(
                incident=incident,
                candidate=candidates.get(sbd),
                sbd_snapshot=sbd,
                relation_type=(
                    IncidentParticipant.RELATION_REPORTED
                    if index == 0
                    else IncidentParticipant.RELATION_MENTIONED
                ),
            )
        )

    IncidentParticipant.objects.bulk_create(participant_rows)
