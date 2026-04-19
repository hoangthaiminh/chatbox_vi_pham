import re
from collections import OrderedDict

from django.db import transaction
from django.db.models.functions import Upper

from .models import Candidate, IncidentParticipant

# SBD patterns (ADD-4): 0–2 letters followed by ≥2 digits, total length 1–9.
#   Examples that match: TS0092, CT983, X123, 7728, 99 (all-digit ≥2 chars).
#   Examples that do NOT match: A1 (only 1 digit), X (no digit), ABC123 (3 letters).
# The (?=.{2,9}$) lookahead caps total length at 9 and guarantees ≥2 chars.
SBD_PATTERN      = re.compile(r"^(?=.{2,9}$)[A-Za-z]{0,2}\d{2,}$")

# For scanning bare SBDs within free text (word-boundary version).
SBD_TEXT_PATTERN = re.compile(r"\b[A-Za-z]{0,2}\d{2,9}\b")

# Explicit mention tokens stored in violation_text: @{TS0031}
# Content inside braces is 1..9 chars of [A-Za-z0-9] (hard cap; validated further below).
MENTION_TOKEN_PATTERN = re.compile(r"@\{([A-Za-z0-9]{1,9})\}")

# Valid SBD syntax: only Latin letters + digits, 1–9 chars.
_SBD_SYNTAX_RE = re.compile(r"^[A-Za-z0-9]{1,9}$")

# Hard cap exposed to other layers so UI/validation stay consistent.
MAX_SBD_LENGTH = 9

# Max violation text length enforced in services (model is TextField, no DB limit)
MAX_VIOLATION_TEXT_LEN = 10_000


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
