from django.db import migrations, models


class Migration(migrations.Migration):
    """Shrink the SBD character fields to max_length=9.

    Safety note for operators: this migration assumes no existing row has an
    SBD longer than 9 characters. If unsure, run a pre-check on the affected
    tables (Candidate.sbd, Incident.reported_sbd, IncidentParticipant.sbd_snapshot)
    before applying.
    """

    dependencies = [
        ("violations", "0002_create_default_groups"),
    ]

    operations = [
        migrations.AlterField(
            model_name="candidate",
            name="sbd",
            field=models.CharField(max_length=9, unique=True),
        ),
        migrations.AlterField(
            model_name="incident",
            name="reported_sbd",
            field=models.CharField(max_length=9),
        ),
        migrations.AlterField(
            model_name="incidentparticipant",
            name="sbd_snapshot",
            field=models.CharField(max_length=9),
        ),
    ]
