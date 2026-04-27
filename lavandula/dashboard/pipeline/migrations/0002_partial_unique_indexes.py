from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("pipeline", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(
            sql=(
                "CREATE UNIQUE INDEX jobs_unique_active_state_phase "
                "ON jobs (state_code, phase) "
                "WHERE state_code IS NOT NULL AND status IN ('pending', 'running');"
            ),
            reverse_sql="DROP INDEX IF EXISTS jobs_unique_active_state_phase;",
        ),
        migrations.RunSQL(
            sql=(
                "CREATE UNIQUE INDEX jobs_unique_active_global_phase "
                "ON jobs (phase) "
                "WHERE state_code IS NULL AND status IN ('pending', 'running');"
            ),
            reverse_sql="DROP INDEX IF EXISTS jobs_unique_active_global_phase;",
        ),
    ]
