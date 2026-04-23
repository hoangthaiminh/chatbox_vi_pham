import re
from collections import OrderedDict

from django.contrib.auth.models import Group
from django.db import transaction
from django.db.models.functions import Upper

from .models import Candidate, Incident, IncidentParticipant, RoomAdminProfile

MAX_SBD_LENGTH = 9
MAX_VIOLATION_TEXT_LEN = 2000

_SBD_SYNTAX_RE = re.compile(rf"^[A-Za-z0-9]{{1,{MAX_SBD_LENGTH}}}$")
# Match TS + 1..N digits up to the max allowed SBD length (reserving 2 chars for 'TS')
digits_max = max(1, MAX_SBD_LENGTH - 2)
SBD_PATTERN = re.compile(rf"\b[Tt][Ss]\d{{1,{digits_max}}}\b")
SBD_TEXT_PATTERN = SBD_PATTERN
# Kept for backwards compatibility with older call-sites.
MENTION_TOKEN_PATTERN = re.compile(r"@\{([A-Za-z0-9]{1,9})\}")

ROLE_SUPER_ADMIN = "super_admin"
ROLE_ROOM_ADMIN = "room_admin"
ROLE_VIEWER = "viewer"
ROLE_CHOICES = (
    (ROLE_SUPER_ADMIN, "Quản trị tổng"),
    (ROLE_ROOM_ADMIN, "Quản trị phòng"),
    (ROLE_VIEWER, "Người xem"),
)
ROLE_LABELS = dict(ROLE_CHOICES)


def normalize_sbd(value):
    return (value or "").upper().strip()


def apply_default_prefix(value):
    """Apply a default TS prefix for digits-only input.

    Returns a tuple of (canonical_sbd, was_truncated).
    """
    norm = normalize_sbd(value)
    if not norm:
        return "", False

    if norm.isdigit():
        prefix = "TS"
        room = MAX_SBD_LENGTH - len(prefix)
        if room < 1:
            return prefix[:MAX_SBD_LENGTH], True
        truncated = len(norm) > room
        if truncated:
            norm = norm[:room]
        return prefix + norm, truncated

    return norm, False


def normalize_and_prefix_sbd(value):
    canonical, _ = apply_default_prefix(value)
    return canonical


def is_valid_sbd_syntax(value):
    """Validate user-entered SBD syntax (latin letters/digits only, 1..9 chars)."""
    return bool(_SBD_SYNTAX_RE.match((value or "").strip()))


def extract_sbd_codes(text):
    """Extract tracked SBDs from plain text.

    Quick-fixes priority: we only track canonical bare SBD tokens (e.g. TS0032)
    and intentionally ignore hoang's @{SBD} tagging semantics.
    """
    ordered = OrderedDict()
    for code in SBD_TEXT_PATTERN.findall(text or ""):
        ordered[normalize_sbd(code)] = True
    return list(ordered.keys())


def normalize_room_name(value):
    return (value or "").strip()


def role_requires_room(role):
    return role == ROLE_ROOM_ADMIN


def ensure_valid_role_room(role, room_name):
    normalized_room_name = normalize_room_name(room_name)
    if role_requires_room(role) and not normalized_room_name:
        raise ValueError(f"Vai trò {ROLE_LABELS[ROLE_ROOM_ADMIN]} bắt buộc phải có tên phòng.")
    return normalized_room_name


def format_role_assignment_success(username, role, room_name=""):
    normalized_room_name = normalize_room_name(room_name)
    role_label = ROLE_LABELS.get(role, role)
    if role == ROLE_ROOM_ADMIN and normalized_room_name:
        return f"Đã gán {username} thành {role_label} cho phòng '{normalized_room_name}'."
    return f"Đã gán {username} thành {role_label}."


def detect_user_role(user):
    if user.groups.filter(name=ROLE_SUPER_ADMIN).exists():
        return ROLE_SUPER_ADMIN
    if user.groups.filter(name=ROLE_ROOM_ADMIN).exists():
        return ROLE_ROOM_ADMIN
    return ROLE_VIEWER


def apply_user_role(user, role, room_name=""):
    normalized_room_name = ensure_valid_role_room(role, room_name)

    super_admin_group, _ = Group.objects.get_or_create(name=ROLE_SUPER_ADMIN)
    room_admin_group, _ = Group.objects.get_or_create(name=ROLE_ROOM_ADMIN)

    user.groups.remove(super_admin_group, room_admin_group)

    if role == ROLE_SUPER_ADMIN:
        user.groups.add(super_admin_group)
        RoomAdminProfile.objects.filter(user=user).delete()
        return

    if role == ROLE_ROOM_ADMIN:
        user.groups.add(room_admin_group)
        RoomAdminProfile.objects.update_or_create(
            user=user,
            defaults={"room_name": normalized_room_name},
        )
        return

    RoomAdminProfile.objects.filter(user=user).delete()


@transaction.atomic
def sync_incident_references(
    incident,
    primary_sbd,
    violation_text,
    incident_kind=Incident.KIND_VIOLATION,
):
    """Save incident + participants using quick_fixes-compatible parsing rules."""
    primary_sbd, primary_truncated = apply_default_prefix(primary_sbd)

    text = (violation_text or "").strip()
    if len(text) > MAX_VIOLATION_TEXT_LEN:
        text = text[:MAX_VIOLATION_TEXT_LEN]

    referenced_codes = extract_sbd_codes(text)

    ordered_codes = [primary_sbd] if primary_sbd else []
    for code in referenced_codes:
        if code != primary_sbd:
            ordered_codes.append(code)

    candidates = {
        candidate.normalized_sbd: candidate
        for candidate in Candidate.objects.annotate(
            normalized_sbd=Upper("sbd")
        ).filter(normalized_sbd__in=ordered_codes)
    } if ordered_codes else {}

    incident.reported_sbd = primary_sbd
    incident.reported_candidate = candidates.get(primary_sbd)
    incident.incident_kind = Incident.normalize_incident_kind(incident_kind)
    incident.violation_text = text
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

    return {
        "primary_sbd_truncated": primary_truncated,
        "mention_truncations": set(),
    }
