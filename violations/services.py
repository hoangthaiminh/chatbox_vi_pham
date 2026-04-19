import re
from collections import OrderedDict

from django.contrib.auth.models import Group
from django.db import transaction
from django.db.models.functions import Upper

from .models import Candidate, IncidentParticipant, RoomAdminProfile

SBD_PATTERN = re.compile(r"\b[Tt][Ss]\d{4}\b")
ROLE_SUPER_ADMIN = "super_admin"
ROLE_ROOM_ADMIN = "room_admin"
ROLE_VIEWER = "viewer"
ROLE_CHOICES = (
    (ROLE_SUPER_ADMIN, "Super Admin"),
    (ROLE_ROOM_ADMIN, "Room Admin"),
    (ROLE_VIEWER, "Viewer"),
)
ROLE_LABELS = dict(ROLE_CHOICES)


def normalize_sbd(value):
    """Uppercase + strip whitespace. Does NOT apply the prefix; callers
    that accept raw user input should use apply_default_prefix afterwards.
    """
    return (value or "").upper().strip()


def apply_default_prefix(value):
    """Attach settings.SBD_DEFAULT_PREFIX when the input is digits-only.

    Returns (canonical_sbd, was_truncated) so the caller can surface a
    warning when a long digits-only input overflowed the 9-char cap and
    the trailing digit(s) were dropped.

    Rules:
      • Empty input → ("", False).
      • All digits, length ≤ MAX_SBD_LENGTH - len(prefix)
                   → (prefix + input, False).
      • All digits, length > that room
                   → (prefix + input[:room], True).  # lossy truncate
      • Has ≥1 letter → (input, False). User supplied their own prefix.

    Examples with prefix 'TS' (room = 7):
      "0032"     → ("TS0032", False)
      "1234567"  → ("TS1234567", False)
      "12345678" → ("TS1234567", True)   trailing digit dropped
      "CT0032"   → ("CT0032", False)
      ""         → ("", False)
    """
    from django.conf import settings
    norm = normalize_sbd(value)
    if not norm:
        return "", False
    if norm.isdigit():
        prefix = getattr(settings, "SBD_DEFAULT_PREFIX", "TS")
        room = MAX_SBD_LENGTH - len(prefix)
        truncated = len(norm) > room
        if truncated:
            norm = norm[:room]
        return prefix + norm, truncated
    return norm, False


def normalize_and_prefix_sbd(value):
    """Thin wrapper that discards the truncation flag. Use apply_default_prefix
    directly when you need to surface a warning to the user."""
    canonical, _ = apply_default_prefix(value)
    return canonical


def is_valid_sbd_syntax(value):
    """Return True if value is a syntactically valid SBD (Latin letters + digits only,
    1–20 chars, no spaces or special characters).
    """
    return bool(_SBD_SYNTAX_RE.match((value or "").strip()))


def extract_sbd_codes(text):
    """Extract SBD codes that should be tracked as incident participants.

    Only explicit @{SBD} tokens count. Each token is run through
    apply_default_prefix so '@{0032}' and '@{TS0032}' are treated as the
    same SBD. The result from apply_default_prefix must still match the
    full SBD_PATTERN to be accepted as a valid participant.

    Returns (ordered_codes, truncated_originals). truncated_originals is
    the set of raw token contents whose trailing digits were dropped by
    the length cap (so the caller can warn the user).
    """
    ordered = OrderedDict()
    truncated_originals = set()
    for raw in MENTION_TOKEN_PATTERN.findall(text or ""):
        canonical, was_truncated = apply_default_prefix(raw)
        if SBD_PATTERN.match(canonical):
            ordered[canonical] = True
            if was_truncated:
                truncated_originals.add(raw)
    return list(ordered.keys()), truncated_originals


def normalize_room_name(value):
    return (value or "").strip()


def role_requires_room(role):
    return role == ROLE_ROOM_ADMIN


def ensure_valid_role_room(role, room_name):
    normalized_room_name = normalize_room_name(room_name)
    if role_requires_room(role) and not normalized_room_name:
        raise ValueError(f"Room name is required for {ROLE_LABELS[ROLE_ROOM_ADMIN]}.")
    return normalized_room_name


def format_role_assignment_success(username, role, room_name=""):
    normalized_room_name = normalize_room_name(room_name)
    role_label = ROLE_LABELS.get(role, role)
    if role == ROLE_ROOM_ADMIN and normalized_room_name:
        return f"{username} set as {role_label} for room '{normalized_room_name}'."
    return f"{username} set as {role_label}."


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
def sync_incident_references(incident, primary_sbd, violation_text):
    """Save the incident, canonicalising SBDs along the way.

    In addition to the obvious normalisation (upper/strip), the service
    applies settings.SBD_DEFAULT_PREFIX to every digits-only token so
    "@{0032}" and "@{TS0032}" end up stored as the same canonical value.
    Violation text is rewritten in-place before saving, so what is kept
    in the DB is the canonical form — template rendering, stats, and
    candidate-detail lookups all stay consistent with what the user sees.

    Returns a dict with metadata the caller may want to surface:
      {
        "primary_sbd_truncated": bool,
        "mention_truncations":   set[str]  # raw token text that was cut
      }
    """
    primary_sbd, primary_truncated = apply_default_prefix(primary_sbd)

    if len(violation_text) > MAX_VIOLATION_TEXT_LEN:
        violation_text = violation_text[:MAX_VIOLATION_TEXT_LEN]

    def _canon_token(match):
        canonical, _ = apply_default_prefix(match.group(1))
        if SBD_PATTERN.match(canonical):
            return "@{" + canonical + "}"
        return match.group(0)

    violation_text = MENTION_TOKEN_PATTERN.sub(_canon_token, violation_text)

    referenced_codes, truncated_originals = extract_sbd_codes(violation_text)

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

    return {
        "primary_sbd_truncated": primary_truncated,
        "mention_truncations": truncated_originals,
    }
