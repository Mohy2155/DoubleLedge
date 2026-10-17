from decimal import Decimal
from django.db import DEFAULT_DB_ALIAS, connection, connections, transaction
from django.utils import timezone
from ledger.models import BalanceSnapshot, TransactionLine, Account, JournalEntry

class BalanceQueryService:
    @staticmethod
    def get_account_balance(account: Account, as_of=None, using=DEFAULT_DB_ALIAS):
        tenant_id = account.tenant_id
        
        # 1. Get latest snapshot up to the as_of date
        snapshot = None
        if as_of:
            snapshot = BalanceSnapshot.objects.using(using).filter(
                tenant_id=tenant_id,
                account=account,
                max_posted_at__lte=as_of
            ).order_by('-sequence').first()
        else:
            snapshot = BalanceSnapshot.objects.using(using).filter(
                tenant_id=tenant_id,
                account=account
            ).order_by('-sequence').first()

        snapshot_used = snapshot is not None
        
        base_debit = snapshot.debit_total if snapshot else Decimal('0.0000')
        base_credit = snapshot.credit_total if snapshot else Decimal('0.0000')
        seq_fence = snapshot.sequence if snapshot else -1

        # 2. Get delta lines
        # delta lines are lines where journal_entry.sequence > seq_fence and journal_entry.posted_at <= as_of
        query = """
            SELECT
                COALESCE(SUM(CASE WHEN l.direction = 'DEBIT' THEN l.amount ELSE 0 END), 0) as delta_debit,
                COALESCE(SUM(CASE WHEN l.direction = 'CREDIT' THEN l.amount ELSE 0 END), 0) as delta_credit
            FROM transaction_lines l
            JOIN journal_entries e ON e.id = l.journal_entry_id
            WHERE l.tenant_id = %s AND l.account_id = %s AND e.sequence > %s
        """
        params = [tenant_id, account.id, seq_fence]
        if as_of:
            query += " AND e.posted_at <= %s"
            params.append(as_of)
            
        with connections[using].cursor() as cursor:
            cursor.execute(query, params)
            row = cursor.fetchone()
            delta_debit = row[0]
            delta_credit = row[1]
            
        total_debit = base_debit + delta_debit
        total_credit = base_credit + delta_credit
        
        # Apply mathematical polarity
        if account.type in [Account.Type.ASSET, Account.Type.EXPENSE]:
            balance = total_debit - total_credit
        else:
            balance = total_credit - total_debit
            
        return {
            'balance': balance,
            'debit_total': total_debit,
            'credit_total': total_credit,
            'snapshot_used': snapshot_used
        }

def generate_account_snapshot(tenant_id, account_id):
    # 1. Temporal buffer: posted_at <= now() - 1 min
    buffer_time = timezone.now() - timezone.timedelta(minutes=1)
    
    with transaction.atomic():
        with connection.cursor() as cursor:
            # pessimistic row lock serialize snapshot workers for this account
            cursor.execute(
                "SELECT id FROM accounts WHERE id = %s AND tenant_id = %s FOR UPDATE SKIP LOCKED", 
                [str(account_id), str(tenant_id)]
            )
            if not cursor.fetchone():
                return  # Locked by another concurrent task or missing

        # Find max sequence before buffer_time using Raw SQL since sequence is unmapped
        query_max = """
            SELECT e.id, e.sequence
            FROM transaction_lines l
            JOIN journal_entries e ON e.id = l.journal_entry_id
            WHERE l.tenant_id = %s AND l.account_id = %s AND e.posted_at <= %s
            ORDER BY e.sequence DESC
            LIMIT 1
        """
        with connection.cursor() as cursor:
            cursor.execute(query_max, [str(tenant_id), str(account_id), buffer_time])
            row = cursor.fetchone()
            if not row:
                return # nothing to snapshot
            entry_id = row[0]
            max_seq = row[1]
            
        if BalanceSnapshot.objects.filter(tenant_id=tenant_id, account_id=account_id, sequence=max_seq).exists():
            return
            
        query = """
            SELECT
                COALESCE(SUM(CASE WHEN l.direction = 'DEBIT' THEN l.amount ELSE 0 END), 0) as total_debit,
                COALESCE(SUM(CASE WHEN l.direction = 'CREDIT' THEN l.amount ELSE 0 END), 0) as total_credit,
                COUNT(l.id) as line_count,
                COALESCE(MAX(e.posted_at), %s) as max_posted_at
            FROM transaction_lines l
            JOIN journal_entries e ON e.id = l.journal_entry_id
            WHERE l.tenant_id = %s AND l.account_id = %s AND e.sequence <= %s
        """
        with connection.cursor() as cursor:
            cursor.execute(query, [buffer_time, str(tenant_id), str(account_id), max_seq])
            row = cursor.fetchone()
            total_debit = row[0]
            total_credit = row[1]
            line_count = row[2]
            max_posted_at = row[3]
            
        account = Account.objects.get(id=account_id)
        if account.type in [Account.Type.ASSET, Account.Type.EXPENSE]:
            balance = total_debit - total_credit
        else:
            balance = total_credit - total_debit
            
        # Use ON CONFLICT DO NOTHING to guarantee idempotent execution without IntegrityError
        insert_query = """
            INSERT INTO balance_snapshots 
            (tenant_id, account_id, sequence, entry_id, balance, debit_total, credit_total, line_count, max_posted_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (tenant_id, account_id, sequence) DO NOTHING
        """
        with connection.cursor() as cursor:
            cursor.execute(insert_query, [
                str(tenant_id), str(account_id), max_seq, str(entry_id),
                balance, total_debit, total_credit, line_count, max_posted_at
            ])
