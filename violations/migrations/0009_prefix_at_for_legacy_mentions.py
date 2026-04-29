# One-shot data migration that brings legacy violation_text into line with
# the new "@SBD" mention rule.
#
# Background: the old SBD_PATTERN was ``\b[Tt][Ss]\d{1,7}\b`` — every bare
# "TS123" run in a chat message was treated as a mention. The new rule is
# strict: only "@TS123" counts (and only when the "@" sits at the start or
# right after whitespace). Existing chat history written under the old rule
# would silently lose its highlighting unless we rewrite it.
#
# This migration scans every Incident.violation_text and prepends an "@"
# in front of any bare TS-style code that:
#   • is preceded by a non-alphanumeric, non-"@" character (or start of the
#     string), so we never break tokens that are part of a larger word
#     (e.g. ``GTS123`` stays ``GTS123``);
#   • is followed by a non-alphanumeric character (or end of string), so
#     we don't catch a TS-prefix inside a longer alphanumeric run.
#
# The pattern explicitly excludes "@" from the lookbehind class, so a text
# already in the new format ("@TS123") is left untouched. That makes the
# migration idempotent — re-running it is a no-op.
from django.db import migrations
import re


# Mirrors the old ``SBD_PATTERN`` (TS + 1..7 digits, case-insensitive),
# wrapped in negative look-around so we only rewrite *clean* boundaries.
_LEGACY_SBD_RE = re.compile(r"(?<![A-Za-z0-9@])([Tt][Ss]\d{1,7})(?![A-Za-z0-9])")


def add_at_prefix(apps, schema_editor):
    Incident = apps.get_model("violations", "Incident")
    # Iterate in chunks so a database with many incidents does not blow up
    # the migration's memory budget.
    for incident in Incident.objects.iterator(chunk_size=200):
        text = incident.violation_text or ""
        if not text:
            continue
        new_text = _LEGACY_SBD_RE.sub(r"@\1", text)
        if new_text != text:
            incident.violation_text = new_text
            incident.save(update_fields=["violation_text"])


def noop_reverse(apps, schema_editor):
    # Forward-only rewrite: stripping the "@" again would also strip "@"
    # marks the user typed deliberately on the new system. Leaving this as
    # a no-op is the safer choice for a downgrade scenario (the new pattern
    # accepts the rewritten form, the old pattern was tolerant of any "@"
    # adjacent to "TS\d+" because the bare suffix still matched its
    # word-boundary-only regex).
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("violations", "0008_incident_kind_add_note"),
    ]

    operations = [
        migrations.RunPython(add_at_prefix, noop_reverse),
    ]
