from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("pipeline", "0002_partial_unique_indexes"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterModelTable(
                    name="report",
                    table="corpus",
                ),
            ],
            database_operations=[],
        ),
    ]
