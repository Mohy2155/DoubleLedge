import sys
import json
from django.core.management.base import BaseCommand
from ledger.services.reconciliation import run_global_reconciliation
from ledger.models import ReconciliationReport

class Command(BaseCommand):
    help = 'Runs the global ledger reconciliation audit'

    def handle(self, *args, **options):
        self.stdout.write("Starting ledger integrity check...")
        reports = run_global_reconciliation()
        
        has_drift = False
        total_accounts = 0
        total_drift = 0
        
        for report in reports:
            total_accounts += report.accounts_checked
            if report.status != ReconciliationReport.Status.CLEAN:
                has_drift = True
                total_drift += len(report.discrepancies)
                self.stderr.write(
                    self.style.ERROR(
                        f"Drift detected for tenant {report.tenant_id}: "
                        f"{json.dumps(report.discrepancies, indent=2)}"
                    )
                )

        if has_drift:
            self.stderr.write(
                self.style.ERROR(
                    f"INTEGRITY FAILURE: {total_drift} discrepancies found across {total_accounts} accounts checked."
                )
            )
            sys.exit(1)
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"INTEGRITY PASSED: 0 discrepancies across {total_accounts} accounts checked."
                )
            )
