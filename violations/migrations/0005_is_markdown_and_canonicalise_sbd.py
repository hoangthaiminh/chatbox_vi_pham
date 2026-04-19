from django.db import migrations, models


def _prefix_digit_sbd(raw, prefix, max_len):
    """Return (canonical, did_change) for a single SBD string."""
    if raw is None:
        return raw, False
    value = raw.upper().strip()
    if not value:
        return value, (value != raw)
    if value.isdigit():
        room = max_len - len(prefix)
        if len(value) > room:
            value = value[:room]
        canonical = prefix + value
    else:
        canonical = value
    return canonical, (canonical != raw)


def canonicalise_all(apps, schema_editor):
    """Re-save every SBD-bearing row using the new prefix rules.

    Run idempotently: if rows are already canonical, nothing is written.
    Also rewrites @{...} tokens inside Incident.violation_text so the
    stored text stays in lockstep with the canonical sbd_snapshot rows.
    """
    from django.conf import settings
    import re as _re

    prefix = getattr(settings, "SBD_DEFAULT_PREFIX", "TS")
    max_len = 9
    token_re = _re.compile(r"@\{([A-Za-z0-9]{1,9})\}")

    def rewrite_text(text):
        if not text:
            return text, False

        changed = [False]

        def repl(m):
            canon, did = _prefix_digit_sbd(m.group(1), prefix, max_len)
            if did:
                changed[0] = True
                return "@{" + canon + "}"
            return m.group(0)

        new = token_re.sub(repl, text)
        return new, changed[0]

    Candidate = apps.get_model("violations", "Candidate")
    Incident = apps.get_model("violations", "Incident")
    IncidentParticipant = apps.get_model("violations", "IncidentParticipant")

    seen = {}
    for c in Candidate.objects.all():
        canon, did = _prefix_digit_sbd(c.sbd, prefix, max_len)
        if did:
            if canon in seen:
                c.delete()
                continue
            seen[canon] = c.pk
            c.sbd = canon
            c.save(update_fields=["sbd"])

    for inc in Incident.objects.all():
        new_sbd, sbd_changed = _prefix_digit_sbd(inc.reported_sbd, prefix, max_len)
        new_text, text_changed = rewrite_text(inc.violation_text)
        fields = []
        if sbd_changed:
            inc.reported_sbd = new_sbd
            fields.append("reported_sbd")
        if text_changed:
            inc.violation_text = new_text
            fields.append("violation_text")
        if fields:
            inc.save(update_fields=fields)

    for p in IncidentParticipant.objects.all():
        canon, did = _prefix_digit_sbd(p.sbd_snapshot, prefix, max_len)
        if did:
            p.sbd_snapshot = canon
            p.save(update_fields=["sbd_snapshot"])


def noop(apps, schema_editor):
    """Data change is not reversible: if we prefixed '0032' to 'TS0032'
    we can't know that the original was '0032' vs 'TS0032'. This is by
    design (the forward migration is one-way cleanup).
    """
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("violations", "0004_add_sbd_indexes"),
    ]

    operations = [
        migrations.AddField(
            model_name="incident",
            name="is_markdown",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "True when the author composed in the expanded Markdown "
                    "editor; False for quick single-line sends where the "
                    "text should be rendered as plain text (with mention "
                    "resolution only)."
                ),
            ),
        ),
        migrations.RunPython(canonicalise_all, noop),
    ]
