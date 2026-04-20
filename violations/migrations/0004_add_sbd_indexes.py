from django.db import migrations, models


class Migration(migrations.Migration):
    """Add DB indexes for the fields we filter/join on most:

    - Incident.reported_sbd: used by the admin filter and potential
      per-SBD queries.
    - IncidentParticipant.sbd_snapshot: used by candidate_detail and
      build_candidate_stats' aggregation.

    With these indexes, candidate-detail rendering stays O(log n) as the
    incident table grows during a real exam season.
    """

    dependencies = [
        ("violations", "0003_shrink_sbd_max_length_to_9"),
    ]

    operations = [
        migrations.AlterField(
            model_name="incident",
            name="reported_sbd",
            field=models.CharField(db_index=True, max_length=9),
        ),
        migrations.AlterField(
            model_name="incidentparticipant",
            name="sbd_snapshot",
            field=models.CharField(db_index=True, max_length=9),
        ),
    ]
