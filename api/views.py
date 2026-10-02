from decimal import Decimal

from django.db import IntegrityError
from django.db.models import Sum
from django.shortcuts import get_object_or_404
from django.utils.dateparse import parse_datetime
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import APIException
from rest_framework.response import Response

from api.decorators import tenant_transaction
from api.serializers import (
    AccountSerializer,
    JournalEntryCreateSerializer,
    JournalEntryReverseSerializer,
    JournalEntrySerializer,
)
from api.utils import generate_idempotency_hash
from ledger.models import Account, JournalEntry, Tenant, TransactionLine
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
    def retrieve(self, request, pk=None):
        tenant_id = request.user.id
        account = get_object_or_404(Account, id=pk, tenant_id=tenant_id)
        serializer = AccountSerializer(account)
        return Response(serializer.data)
        
    @tenant_transaction()
    @action(detail=True, methods=['get'])
    def balance(self, request, pk=None):
        tenant_id = request.user.id
        account = get_object_or_404(Account, id=pk, tenant_id=tenant_id)
        
        as_of_str = request.query_params.get('as_of')
        if not as_of_str:
            return Response({"error": {"code": "invalid", "message": "as_of parameter is required"}}, status=400)
            
        as_of = parse_datetime(as_of_str)
        if not as_of:
            return Response({"error": {"code": "invalid", "message": "invalid as_of datetime format"}}, status=400)
        
        if as_of.tzinfo is None:
            return Response({"error": {"code": "invalid", "message": "as_of must be timezone-aware"}}, status=400)
            
        lines = TransactionLine.objects.filter(
            tenant_id=tenant_id,
            account=account,
            journal_entry__effective_at__lte=as_of
        )
        
        debits = lines.filter(direction='DEBIT').aggregate(total=Sum('amount'))['total'] or Decimal(0)
        credits = lines.filter(direction='CREDIT').aggregate(total=Sum('amount'))['total'] or Decimal(0)
        
        if account.type in ('ASSET', 'EXPENSE'):
            balance = debits - credits
        else:
            balance = credits - debits
            
        return Response({
            "account_id": str(account.id),
            "balance": str(balance),
            "as_of": as_of.isoformat()
        })
        
    @tenant_transaction()
    @action(detail=True, methods=['get'])
    def statement(self, request, pk=None):
        tenant_id = request.user.id
        account = get_object_or_404(Account, id=pk, tenant_id=tenant_id)
        
        limit = int(request.query_params.get('limit', 100))
        cursor_id = request.query_params.get('cursor')
        
        lines_qs = TransactionLine.objects.filter(tenant_id=tenant_id, account=account).order_by('id')
        if cursor_id:
            lines_qs = lines_qs.filter(id__gt=cursor_id)
            
        lines = list(lines_qs[:limit])
        
        if cursor_id:
            prior_lines = TransactionLine.objects.filter(tenant_id=tenant_id, account=account, id__lte=cursor_id)
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
