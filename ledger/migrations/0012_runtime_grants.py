 from django.db import migrations


    class Migration(migrations.Migration):

        dependencies = [
            ('ledger', '0011_tenant_lister'),
        ]

        operations = [
            migrations.RunSQL(
                sql="""
                DO $$
                BEGIN
                    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ledger_runtime') THEN   
                        GRANT USAGE ON SCHEMA public TO ledger_runtime;
                        GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA public TO
  ledger_runtime;
                        GRANT UPDATE ON accounts, holds, webhook_deliveries, outbox_events,    
  idempotency_keys, webhook_endpoints TO ledger_runtime;
                        GRANT DELETE ON webhook_endpoints, webhook_deliveries, outbox_events   
  TO ledger_runtime;
                        GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO
  ledger_runtime;
                        IF EXISTS (SELECT 1 FROM pg_proc WHERE proname = 'list_tenant_ids')    
  THEN
                            GRANT EXECUTE ON FUNCTION public.list_tenant_ids() TO
  ledger_runtime;
                        END IF;
                    ELSE
                        RAISE NOTICE 'Role ledger_runtime does not exist; skipping runtime     
  grants.';
                    END IF;
                END
                $$;
                """,
                reverse_sql="""
                DO $$
                BEGIN
                    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ledger_runtime') THEN   
                        REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM
  ledger_runtime;
                        REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM
  ledger_runtime;
                        REVOKE USAGE ON SCHEMA public FROM ledger_runtime;
                    END IF;
                END
                $$;
                """
            )
        ]
