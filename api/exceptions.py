from django.db.models import ObjectDoesNotExist
from rest_framework.response import Response
from rest_framework.views import exception_handler

from ledger.services.posting import PostingError


def custom_exception_handler(exc, context):
    # Call REST framework's default exception handler first,
    # to get the standard error response.
    response = exception_handler(exc, context)

    if response is None:
        if isinstance(exc, PostingError):
            message = str(exc)
            
            error_mapping = {
                "idempotency_key_reuse": (422, "idempotency_key_reuse"),
                "unbalanced_entry": (422, "unbalanced_entry"),
                "invalid_amount_format": (422, "invalid_amount_format"),
                "invalid_amount_precision": (422, "invalid_amount_precision"),
                "invalid_amount": (422, "invalid_amount"),
                "insufficient_funds": (422, "insufficient_funds"),
                "currency_mismatch": (422, "currency_mismatch"),
                "One or more accounts not found": (404, "not_found"),
                "reference_conflict": (409, "reference_conflict"),
                "already_reversed": (409, "already_reversed"),
            }
            
            # Default to 422 if not found in mapping
            status_code, code = error_mapping.get(message, (422, "unprocessable_entity"))
            return Response({"error": {"code": code, "message": message}}, status=status_code)
            
        if isinstance(exc, ObjectDoesNotExist):
            return Response({"error": {"code": "not_found", "message": "Resource not found"}}, status=404)
            
    else:
        # Standardize DRF exceptions
        code = response.status_code
        error_code = "error"
        if code == 400:
            error_code = getattr(exc, 'default_code', 'invalid')
            # Custom missing idempotency key handling
            if "Idempotency-Key" in str(response.data):
                error_code = "missing_idempotency_key"
        elif code == 401:
            error_code = getattr(exc, 'default_code', 'unauthorized')
            if isinstance(response.data, dict) and response.data.get('detail') == 'Authentication credentials were not provided.':
                error_code = 'unauthorized'
        elif code == 403:
            error_code = "permission_denied"
        elif code == 404:
            error_code = "not_found"
        elif code == 429:
            error_code = "too_many_requests"
        elif code == 503:
            error_code = "lock_timeout"
            
        data = response.data
        if isinstance(data, dict) and "error" not in data:
            response.data = {"error": {"code": error_code, "message": data}}
        elif not isinstance(data, dict):
            response.data = {"error": {"code": error_code, "message": str(data)}}
            
        # Add Retry-After header for 503
        if code == 503:
            response['Retry-After'] = '1'

    return response
