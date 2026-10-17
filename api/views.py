from decimal import Decimal

from django.db import IntegrityError
from django.db.models import Sum
from django.shortcuts import get_object_or_404
from django.utils.dateparse import parse_datetime
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.response import Response

from api.decorators import tenant_transaction
from api.serializers import (
    AccountSerializer,
    JournalEntryCreateSerializer,
    JournalEntryReverseSerializer,
    JournalEntrySerializer,
    PeriodCloseCreateSerializer,
    PeriodCloseSerializer,
)
from api.utils import generate_idempotency_hash
from ledger.models import Account, JournalEntry, PeriodClose, Tenant, TransactionLine
from ledger.services.periods import request_close
from ledger.services.replica import CONSISTENCY_LEVELS, REPLICA_ALIAS, tenant_reads
from ledger.services.posting import PostingError, post_entry


class MissingIdempotencyKey(APIException):
    status_code = 400
    default_detail = 'Idempotency-Key header is required.'
    default_code = 'missing_idempotency_key'

class JournalEntryViewSet(viewsets.ViewSet):
    
    @tenant_transaction()
    def create(self, request):
        idempotency_key = request.META.get('HTTP_IDEMPOTENCY_KEY')
        if not idempotency_key:
            raise MissingIdempotencyKey()
            
        serializer = JournalEntryCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        request_hash = generate_idempotency_hash(request.method, request.path, request.body)
        tenant = Tenant(id=request.user.id)
        
        code, body, is_replayed = post_entry(tenant, serializer.validated_data, idempotency_key, request_hash)
        
        headers = {}
        if is_replayed:
            headers['Idempotent-Replayed'] = 'true'
            
        return Response(body, status=code, headers=headers)
        
    @tenant_transaction()
    def retrieve(self, request, pk=None):
        tenant_id = request.user.id
        entry = get_object_or_404(JournalEntry.objects.prefetch_related('lines'), id=pk, tenant_id=tenant_id)
        
        from ledger.services.sharding import to_logical_account_id
        import uuid
        line_accounts = [line.account_id for line in entry.lines.all()]
        mapping = to_logical_account_id(tenant_id, line_accounts)
        for line in entry.lines.all():
            line.account_id = uuid.UUID(mapping[str(line.account_id)])
            
        serializer = JournalEntrySerializer(entry)
        return Response(serializer.data)
        
    @tenant_transaction()
    @action(detail=True, methods=['post'])
    def reverse(self, request, pk=None):
        tenant_id = request.user.id
        entry = get_object_or_404(JournalEntry.objects.prefetch_related('lines'), id=pk, tenant_id=tenant_id)
        
        idempotency_key = request.META.get('HTTP_IDEMPOTENCY_KEY')
        if not idempotency_key:
            raise MissingIdempotencyKey()
            
        serializer = JournalEntryReverseSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        reference_id = serializer.validated_data.get('reference_id', f"reversal:{entry.id}")
        
        try:
            if entry.reversed_by:
                raise PostingError("already_reversed")
        except JournalEntry.DoesNotExist:
            pass
            
        lines_data = []
        for line in entry.lines.all():
            lines_data.append({
                'account_id': str(line.account_id),
                'amount': str(line.amount),
                'direction': 'CREDIT' if line.direction == 'DEBIT' else 'DEBIT'
            })
            
        payload = {
            'reference_id': reference_id,
            'description': f"Reversal of {entry.reference_id}",
            'lines': lines_data,
            'reverses_id': str(entry.id)
        }
        
        request_hash = generate_idempotency_hash(request.method, request.path, request.body)
        tenant = Tenant(id=request.user.id)
        
        try:
            code, body, is_replayed = post_entry(tenant, payload, idempotency_key, request_hash)
        except IntegrityError:
            raise PostingError("already_reversed")
            
        headers = {}
        if is_replayed:
            headers['Idempotent-Replayed'] = 'true'
            
        return Response(body, status=code, headers=headers)

class AccountViewSet(viewsets.ViewSet):
    
    @tenant_transaction()
    def list(self, request):
        tenant_id = request.user.id
        from ledger.services.sharding import get_aggregate_balance

        accounts = Account.objects.filter(
            tenant_id=tenant_id,
            parent_account_id__isnull=True
        ).order_by('name')[:100]

        results = []
        for account in accounts:
            aggs = get_aggregate_balance(tenant_id, account.id)
            account.posted_balance = aggs['posted_balance']
            account.pending_holds = aggs['pending_holds']
            account.available_balance = aggs['available_balance']
            results.append(account)

        serializer = AccountSerializer(results, many=True)
        return Response(serializer.data)

    @tenant_transaction()
    def retrieve(self, request, pk=None):
        tenant_id = request.user.id
        account = get_object_or_404(Account, id=pk, tenant_id=tenant_id)
        
        if account.parent_account_id is not None:
            from django.http import Http404
            raise Http404("No Account matches the given query.")
            
        from ledger.services.sharding import get_aggregate_balance
        
        aggs = get_aggregate_balance(tenant_id, account.id)
        
        account.posted_balance = aggs['posted_balance']
        account.pending_holds = aggs['pending_holds']
        account.available_balance = aggs['available_balance']
        
        serializer = AccountSerializer(account)
        return Response(serializer.data)
        
    @tenant_transaction()
    @action(detail=True, methods=['get'])
    def balance(self, request, pk=None):
        return self._routed_read(request, pk, self._balance)

    @tenant_transaction()
    @action(detail=True, methods=['get'])
    def statement(self, request, pk=None):
        return self._routed_read(request, pk, self._statement)

    def _routed_read(self, request, pk, read):
        """Serves a history read from the replica when it is fresh enough (ledger.services.replica)."""
        consistency = request.query_params.get('consistency', 'bounded')
        if consistency not in CONSISTENCY_LEVELS:
            raise ValidationError({"consistency": f"must be one of: {', '.join(CONSISTENCY_LEVELS)}"})
        with tenant_reads(request.user.id, consistency) as alias:
            response = read(request, pk, alias)
        response['Read-Source'] = 'replica' if alias == REPLICA_ALIAS else 'primary'
        return response

    def _balance(self, request, pk, alias):
        tenant_id = request.user.id
        account = get_object_or_404(Account.objects.using(alias), id=pk, tenant_id=tenant_id)
        
        if account.parent_account_id is not None:
            from django.http import Http404
            raise Http404("No Account matches the given query.")
            
        as_of_str = request.query_params.get('as_of')
        as_of = None
        if as_of_str:
            as_of = parse_datetime(as_of_str)
            if not as_of:
                return Response({"error": {"code": "invalid", "message": "invalid as_of datetime format"}}, status=400)
            if as_of.tzinfo is None:
                return Response({"error": {"code": "invalid", "message": "as_of must be timezone-aware"}}, status=400)
            
        from ledger.services.snapshots import BalanceQueryService
        from ledger.services.sharding import resolve_member_ids
        from api.serializers import AccountBalanceSerializer
        from decimal import Decimal
        
        member_ids = resolve_member_ids(tenant_id, account.id, using=alias)
        
        total_balance = Decimal('0.0000')
        total_debit = Decimal('0.0000')
        total_credit = Decimal('0.0000')
        snapshot_used = False
        
        for member_id in member_ids:
            # We need the Account instance for BalanceQueryService
            member_acc = Account.objects.using(alias).get(tenant_id=tenant_id, id=member_id)
            result = BalanceQueryService.get_account_balance(member_acc, as_of=as_of, using=alias)
            total_balance += Decimal(result['balance'])
            total_debit += Decimal(result['debit_total'])
            total_credit += Decimal(result['credit_total'])
            if result.get('snapshot_used'):
                snapshot_used = True
                
        aggregated_result = {
            "balance": str(total_balance),
            "debit_total": str(total_debit),
            "credit_total": str(total_credit),
            "snapshot_used": snapshot_used
        }
        
        serializer = AccountBalanceSerializer(aggregated_result)
        return Response(serializer.data)
        
    def _statement(self, request, pk, alias):
        tenant_id = request.user.id
        account = get_object_or_404(Account.objects.using(alias), id=pk, tenant_id=tenant_id)
        
        if account.parent_account_id is not None:
            from django.http import Http404
            raise Http404("No Account matches the given query.")
            
        limit = int(request.query_params.get('limit', 100))
        cursor_id = request.query_params.get('cursor')
        
        from ledger.services.sharding import resolve_member_ids
        from ledger.services.posting import exclude_internal_lines
        member_ids = resolve_member_ids(tenant_id, account.id, using=alias)
        
        lines_qs = TransactionLine.objects.using(alias).filter(tenant_id=tenant_id, account_id__in=member_ids).order_by('id')
        lines_qs = exclude_internal_lines(lines_qs)
        
        if cursor_id:
            lines_qs = lines_qs.filter(id__gt=cursor_id)
            
        lines = list(lines_qs[:limit])
        
        if cursor_id:
            prior_lines = TransactionLine.objects.using(alias).filter(tenant_id=tenant_id, account_id__in=member_ids, id__lte=cursor_id)
            prior_lines = exclude_internal_lines(prior_lines)
            
            debits = prior_lines.filter(direction='DEBIT').aggregate(total=Sum('amount'))['total'] or Decimal(0)
            credits = prior_lines.filter(direction='CREDIT').aggregate(total=Sum('amount'))['total'] or Decimal(0)
            
            if account.type in ('ASSET', 'EXPENSE'):
                running_balance = debits - credits
            else:
                running_balance = credits - debits
        else:
            running_balance = Decimal(0)
            
        results = []
        for line in lines:
            if account.type in ('ASSET', 'EXPENSE'):
                if line.direction == 'DEBIT':
                    running_balance += line.amount
                else:
                    running_balance -= line.amount
            else:
                if line.direction == 'CREDIT':
                    running_balance += line.amount
                else:
                    running_balance -= line.amount
                    
            results.append({
                "id": line.id,
                "journal_entry_id": str(line.journal_entry_id),
                "account_id": str(account.id), # logical mapping
                "direction": line.direction,
                "amount": str(line.amount),
                "running_balance": str(running_balance)
            })
            
        next_cursor = results[-1]["id"] if results else None
        
        return Response({
            "results": results,
            "next_cursor": next_cursor
        })

import secrets
from api.serializers import WebhookEndpointSerializer, WebhookEndpointCreateSerializer
from ledger.models import WebhookEndpoint

class WebhookEndpointViewSet(viewsets.ViewSet):
    
    @tenant_transaction()
    def create(self, request):
        serializer = WebhookEndpointSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        tenant_id = request.user.id
        # Check duplicate url
        if WebhookEndpoint.objects.filter(tenant_id=tenant_id, url=serializer.validated_data['url']).exists():
            return Response({"error": {"code": "conflict", "message": "Webhook endpoint with this URL already exists."}}, status=409)
            
        secret = f"whsec_{secrets.token_urlsafe(32)}"
        
        endpoint = WebhookEndpoint.objects.create(
            tenant_id=tenant_id,
            url=serializer.validated_data['url'],
            secret=secret,
            event_types=serializer.validated_data['event_types'],
            is_active=serializer.validated_data.get('is_active', True)
        )
        
        data = WebhookEndpointCreateSerializer(endpoint).data
        return Response(data, status=201)

    @tenant_transaction()
    def list(self, request):
        tenant_id = request.user.id
        endpoints = WebhookEndpoint.objects.filter(tenant_id=tenant_id)
        return Response(WebhookEndpointSerializer(endpoints, many=True).data)

    @tenant_transaction()
    def retrieve(self, request, pk=None):
        tenant_id = request.user.id
        endpoint = get_object_or_404(WebhookEndpoint, tenant_id=tenant_id, pk=pk)
        return Response(WebhookEndpointSerializer(endpoint).data)

    @tenant_transaction()
    def update(self, request, pk=None):
        tenant_id = request.user.id
        endpoint = get_object_or_404(WebhookEndpoint, tenant_id=tenant_id, pk=pk)
        
        serializer = WebhookEndpointSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        
        if 'url' in serializer.validated_data and serializer.validated_data['url'] != endpoint.url:
            if WebhookEndpoint.objects.filter(tenant_id=tenant_id, url=serializer.validated_data['url']).exclude(pk=pk).exists():
                return Response({"error": {"code": "conflict", "message": "Webhook endpoint with this URL already exists."}}, status=409)
            endpoint.url = serializer.validated_data['url']
            
        if 'event_types' in serializer.validated_data:
            endpoint.event_types = serializer.validated_data['event_types']
            
        if 'is_active' in serializer.validated_data:
            endpoint.is_active = serializer.validated_data['is_active']
            
        endpoint.save()
        return Response(WebhookEndpointSerializer(endpoint).data)

    @tenant_transaction()
    def destroy(self, request, pk=None):
        tenant_id = request.user.id
        endpoint = get_object_or_404(WebhookEndpoint, tenant_id=tenant_id, pk=pk)
        endpoint.delete()
        return Response(status=204)

from ledger.services.holds import authorize_hold, capture_hold, void_hold
from ledger.models import Hold
from api.serializers import HoldSerializer, HoldCreateSerializer, HoldCaptureSerializer

class HoldViewSet(viewsets.ViewSet):
    
    @tenant_transaction()
    def list(self, request):
        tenant_id = request.user.id
        status_filter = request.query_params.get('status')

        qs = Hold.objects.filter(tenant_id=tenant_id)
        if status_filter:
            qs = qs.filter(status=status_filter)
        holds = qs.order_by('-created_at')[:100]

        serializer = HoldSerializer(holds, many=True)
        return Response(serializer.data)

    @tenant_transaction()
    def create(self, request):
        idempotency_key = request.META.get('HTTP_IDEMPOTENCY_KEY')
        if not idempotency_key:
            raise MissingIdempotencyKey()
            
        serializer = HoldCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        request_hash = generate_idempotency_hash(request.method, request.path, request.body)
        tenant = Tenant(id=request.user.id)
        
        code, body, is_replayed = authorize_hold(tenant, serializer.validated_data, idempotency_key, request_hash)
            
        headers = {}
        if is_replayed:
            headers['Idempotent-Replayed'] = 'true'
            
        return Response(body, status=code, headers=headers)
        
    @tenant_transaction()
    def retrieve(self, request, pk=None):
        tenant_id = request.user.id
        hold = get_object_or_404(Hold, id=pk, tenant_id=tenant_id)
        serializer = HoldSerializer(hold)
        return Response(serializer.data)
        
    @tenant_transaction()
    @action(detail=True, methods=['post'])
    def capture(self, request, pk=None):
        idempotency_key = request.META.get('HTTP_IDEMPOTENCY_KEY')
        if not idempotency_key:
            raise MissingIdempotencyKey()
            
        serializer = HoldCaptureSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        request_hash = generate_idempotency_hash(request.method, request.path, request.body)
        tenant = Tenant(id=request.user.id)
        
        code, body, is_replayed = capture_hold(tenant, pk, serializer.validated_data, idempotency_key, request_hash)
            
        headers = {}
        if is_replayed:
            headers['Idempotent-Replayed'] = 'true'
            
        return Response(body, status=code, headers=headers)
        
    @tenant_transaction()
    @action(detail=True, methods=['post'])
    def void(self, request, pk=None):
        idempotency_key = request.META.get('HTTP_IDEMPOTENCY_KEY')
        if not idempotency_key:
            raise MissingIdempotencyKey()
            
        request_hash = generate_idempotency_hash(request.method, request.path, request.body)
        tenant = Tenant(id=request.user.id)
        
        code, body, is_replayed = void_hold(tenant, pk, idempotency_key, request_hash)
            
        headers = {}
        if is_replayed:
            headers['Idempotent-Replayed'] = 'true'
            
        return Response(body, status=code, headers=headers)

from ledger.services.fx import conversion_body, conversions_with_lines, convert
from api.serializers import FxConversionCreateSerializer

class FxConversionViewSet(viewsets.ViewSet):

    @tenant_transaction()
    def list(self, request):
        conversions = conversions_with_lines(request.user.id).order_by('-created_at')[:100]
        return Response([conversion_body(c) for c in conversions])

    @tenant_transaction()
    def retrieve(self, request, pk=None):
        conversion = get_object_or_404(conversions_with_lines(request.user.id), id=pk)
        return Response(conversion_body(conversion))

    @tenant_transaction()
    def create(self, request):
        idempotency_key = request.META.get('HTTP_IDEMPOTENCY_KEY')
        if not idempotency_key:
            raise MissingIdempotencyKey()

        serializer = FxConversionCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        request_hash = generate_idempotency_hash(request.method, request.path, request.body)
        tenant = Tenant(id=request.user.id)

        code, body, is_replayed = convert(tenant, serializer.validated_data, idempotency_key, request_hash)

        headers = {}
        if is_replayed:
            headers['Idempotent-Replayed'] = 'true'

        return Response(body, status=code, headers=headers)

class PeriodCloseViewSet(viewsets.ViewSet):

    @tenant_transaction()
    def list(self, request):
        closes = PeriodClose.objects.filter(tenant_id=request.user.id).order_by('-cutoff')
        results = PeriodCloseSerializer(closes, many=True).data
        # Cutoffs only move forward, so the newest close holds the current cutoff.
        return Response({
            "current_cutoff": results[0]["cutoff"] if results else None,
            "results": results,
        })

    @tenant_transaction()
    def create(self, request):
        idempotency_key = request.META.get('HTTP_IDEMPOTENCY_KEY')
        if not idempotency_key:
            raise MissingIdempotencyKey()

        serializer = PeriodCloseCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        request_hash = generate_idempotency_hash(request.method, request.path, request.body)
        tenant = Tenant(id=request.user.id)

        code, body, is_replayed = request_close(tenant, serializer.validated_data['cutoff'], idempotency_key, request_hash)

        headers = {}
        if is_replayed:
            headers['Idempotent-Replayed'] = 'true'

        return Response(body, status=code, headers=headers)
