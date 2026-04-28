from django.db.models import Count, Max, OuterRef, Q, Subquery
from django.db.models.functions import Upper
from django.template.loader import render_to_string
from django.utils import timezone

from .models import Candidate, Incident, IncidentParticipant

INCIDENT_PAGE_SIZE = 30
INCIDENT_UPDATE_LIMIT = 80


def can_delete_incidents(user):
    if not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    return user.groups.filter(name__in=["super_admin", "room_admin"]).exists()


def build_candidate_stats():
    latest_violation = Incident.objects.filter(
        participants__candidate=OuterRef("pk")
    ).order_by("-created_at")

    stats_qs = Candidate.objects.annotate(
        violation_count=Count(
            "incident_links__incident",
            filter=Q(
                incident_links__incident__incident_kind=Incident.KIND_VIOLATION,
            ),
            distinct=True,
        ),
        reminder_count=Count(
            "incident_links__incident",
            filter=Q(
                incident_links__incident__incident_kind=Incident.KIND_REMINDER,
            ),
            distinct=True,
        ),
        last_violation_at=Max("incident_links__incident__created_at"),
        latest_violation_text=Subquery(latest_violation.values("violation_text")[:1]),
    ).filter(
        Q(violation_count__gt=0) | Q(reminder_count__gt=0)
    )

    stats = []
    for row in stats_qs:
        effective_violations = int(row.violation_count) + int(row.reminder_count) // 2
        stats.append(
            {
                "sbd": row.sbd,
                "full_name": row.full_name,
                "school": row.school,
                "exam_room": row.exam_room,
                "supervisor_teacher": row.supervisor_teacher,
                "violation_count": int(row.violation_count),
                "reminder_count": int(row.reminder_count),
                "effective_violations": effective_violations,
                "is_out": effective_violations >= 2,
                "last_violation_at": row.last_violation_at,
                "latest_violation_text": row.latest_violation_text,
            }
        )
    stats.sort(
        key=lambda item: (
            -item["effective_violations"],
            -item["violation_count"],
            -item["reminder_count"],
            item["sbd"],
        )
    )

    unknown_qs = (
        IncidentParticipant.objects.filter(candidate__isnull=True)
        .annotate(normalized_sbd=Upper("sbd_snapshot"))
        .values("normalized_sbd")
        .annotate(
            violation_count=Count(
                "incident",
                filter=Q(incident__incident_kind=Incident.KIND_VIOLATION),
                distinct=True,
            ),
            reminder_count=Count(
                "incident",
                filter=Q(incident__incident_kind=Incident.KIND_REMINDER),
                distinct=True,
            ),
            last_violation_at=Max("incident__created_at"),
        )
        .filter(Q(violation_count__gt=0) | Q(reminder_count__gt=0))
    )

    unknown_stats = []
    for row in unknown_qs:
        effective_violations = int(row["violation_count"]) + int(row["reminder_count"]) // 2
        unknown_stats.append(
            {
                "normalized_sbd": row["normalized_sbd"],
                "violation_count": int(row["violation_count"]),
                "reminder_count": int(row["reminder_count"]),
                "effective_violations": effective_violations,
                "is_out": effective_violations >= 2,
                "last_violation_at": row["last_violation_at"],
            }
        )
    unknown_stats.sort(
        key=lambda item: (
            -item["effective_violations"],
            -item["violation_count"],
            -item["reminder_count"],
            item["normalized_sbd"],
        )
    )

    return stats, unknown_stats


def fetch_incidents_page(before_id=None, after_id=None, limit=INCIDENT_PAGE_SIZE):
    query = Incident.objects.select_related("created_by", "reported_candidate").prefetch_related(
        "participants__candidate"
    )

    if after_id is not None:
        return list(query.filter(id__gt=after_id).order_by("id")[:limit])

    if before_id is not None:
        incidents = list(query.filter(id__lt=before_id).order_by("-id")[:limit])
        incidents.reverse()
        return incidents

    incidents = list(query.order_by("-id")[:limit])
    incidents.reverse()
    return incidents


def get_editable_incident_ids(incidents, user):
    if not getattr(user, "is_authenticated", False):
        return []
    return [incident.id for incident in incidents if incident.can_edit(user)]


def get_deletable_incident_ids(incidents, user):
    """Return the subset of ``incidents`` IDs that ``user`` may delete.

    Mirrors ``get_editable_incident_ids`` but uses ``Incident.can_delete``,
    so room admins do NOT see a delete button on a super admin's posts.
    """
    if not getattr(user, "is_authenticated", False):
        return []
    return [incident.id for incident in incidents if incident.can_delete(user)]


def render_incident_rows_html(incidents, user):
    return render_to_string(
        "violations/_incident_rows.html",
        {
            "incidents": incidents,
            "editable_incident_ids": get_editable_incident_ids(incidents, user),
            "deletable_incident_ids": get_deletable_incident_ids(incidents, user),
            "current_user_id": user.id if getattr(user, "is_authenticated", False) else None,
            "can_delete_incidents": can_delete_incidents(user),
        },
    )


def build_stats_payload():
    candidate_stats, unknown_stats = build_candidate_stats()
    return {
        "stats_html": render_to_string(
            "violations/_stats_table.html",
            {
                "candidate_stats": candidate_stats,
                "unknown_stats": unknown_stats,
            },
        ),
        "timestamp": timezone.now().isoformat(),
    }


def build_live_payload(user):
    incidents = fetch_incidents_page(limit=INCIDENT_PAGE_SIZE)
    oldest_id = incidents[0].id if incidents else None
    newest_id = incidents[-1].id if incidents else None

    payload = build_stats_payload()
    payload.update(
        {
            "incidents_html": render_incident_rows_html(incidents, user),
            "oldest_id": oldest_id,
            "newest_id": newest_id,
            "has_older": Incident.objects.filter(id__lt=oldest_id).exists() if oldest_id else False,
        }
    )
    return payload
