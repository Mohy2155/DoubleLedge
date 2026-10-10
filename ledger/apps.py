import os
import sys
import logging
from django.apps import AppConfig

logger = logging.getLogger(__name__)


class LedgerConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'ledger'

    def ready(self):
        # Skip during tests and management commands that run as admin
        if 'pytest' in sys.modules or 'test' in sys.argv:
            return
        if any(cmd in sys.argv for cmd in ['migrate', 'makemigrations', 'collectstatic', 'createsuperuser']):
            return

        try:
            from django.db import connection
            with connection.cursor() as cursor:
                cursor.execute("""
                    SELECT rolname, rolsuper, rolbypassrls 
                    FROM pg_roles 
                    WHERE rolname = current_user;
                """)
                row = cursor.fetchone()
                if row:
                    rolname, is_super, is_bypass = row
                    if is_super or is_bypass:
                        msg = (
                            f"DoubleLedge database role violation: default connection is running as privileged role "
                            f"'{rolname}' (superuser={is_super}, bypassrls={is_bypass}). "
                            f"Production runtime must use restricted role 'ledger_runtime'."
                        )
                        logger.error(msg)
                        if os.environ.get("REQUIRE_RESTRICTED_DB_ROLE") == "1":
                            raise RuntimeError(msg)
        except Exception as e:
            if os.environ.get("REQUIRE_RESTRICTED_DB_ROLE") == "1":
                raise
            logger.warning(f"Could not verify database role privileges at startup: {e}")

