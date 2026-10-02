from django.db import migrations

class Migration(migrations.Migration):

    dependencies = [
        ('ledger', '0002_models'),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
            CREATE TABLE reconciliation_reports (
                id uuid PRIMARY KEY,
                tenant_id uuid NOT NULL REFERENCES tenants(id),
                timestamp timestamptz NOT NULL DEFAULT now(),
                status text NOT NULL CHECK (status IN ('CLEAN', 'DRIFT_DETECTED')),
                accounts_checked int NOT NULL,
                discrepancies jsonb NOT NULL DEFAULT '{}'
            );

            ALTER TABLE reconciliation_reports ENABLE ROW LEVEL SECURITY;
            ALTER TABLE reconciliation_reports FORCE ROW LEVEL SECURITY;
            CREATE POLICY tenant_isolation_reconciliation_reports ON reconciliation_reports
                USING      (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
                WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
            """,
            reverse_sql="""
            DROP TABLE IF EXISTS reconciliation_reports CASCADE;
            """
        )
    ]
