from django.db import migrations, models

class Migration(migrations.Migration):

    dependencies = [
        ('ledger', '0008_holds'),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
            ALTER TABLE accounts ADD COLUMN shard_count INTEGER NOT NULL DEFAULT 0;
            ALTER TABLE accounts ADD COLUMN parent_account_id UUID;
            
            ALTER TABLE accounts ADD CONSTRAINT accounts_shard_count_check CHECK (shard_count >= 0);
            ALTER TABLE accounts ADD CONSTRAINT accounts_parent_shard_check CHECK (
                (parent_account_id IS NULL AND shard_count >= 0) OR
                (parent_account_id IS NOT NULL AND shard_count = 0)
            );
            
            ALTER TABLE accounts ADD CONSTRAINT accounts_parent_fk 
                FOREIGN KEY (tenant_id, parent_account_id) 
                REFERENCES accounts (tenant_id, id);
            """,
            reverse_sql="""
            ALTER TABLE accounts DROP CONSTRAINT IF EXISTS accounts_parent_fk;
            ALTER TABLE accounts DROP CONSTRAINT IF EXISTS accounts_parent_shard_check;
            ALTER TABLE accounts DROP CONSTRAINT IF EXISTS accounts_shard_count_check;
            ALTER TABLE accounts DROP COLUMN IF EXISTS parent_account_id;
            ALTER TABLE accounts DROP COLUMN IF EXISTS shard_count;
            """
        ),
    ]
