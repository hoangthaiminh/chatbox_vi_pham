from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("violations", "0005_is_markdown_and_canonicalise_sbd"),
    ]

    operations = [
        migrations.AddField(
            model_name="incident",
            name="incident_kind",
            field=models.CharField(
                choices=[("violation", "Vi phạm"), ("reminder", "Nhắc nhở")],
                db_index=True,
                default="violation",
                max_length=20,
            ),
        ),
    ]
