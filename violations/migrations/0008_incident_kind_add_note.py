# Hand-written for the production deployment: extends ``Incident.incident_kind``
# with a third choice ``"note"`` ("Ghi chú"). No DB schema change is strictly
# required (the column is already a CharField(max_length=20) with no DB-level
# CHECK constraint), but we still register an AlterField so Django's choices
# validator stays in sync with the model and any future ``makemigrations``
# pass does not produce a duplicate.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("violations", "0007_alter_roomadminprofile_options_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="incident",
            name="incident_kind",
            field=models.CharField(
                choices=[
                    ("violation", "Vi phạm"),
                    ("reminder", "Nhắc nhở"),
                    ("note", "Ghi chú"),
                ],
                db_index=True,
                default="violation",
                max_length=20,
            ),
        ),
    ]
