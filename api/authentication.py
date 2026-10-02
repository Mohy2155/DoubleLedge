import uuid

import jwt
from django.conf import settings
from rest_framework import authentication, exceptions


class TenantUser:
    def __init__(self, tenant_id):
        self.id = tenant_id
        self.is_authenticated = True

class TenantJWTAuthentication(authentication.BaseAuthentication):
    def authenticate(self, request):
        auth_header = request.META.get('HTTP_AUTHORIZATION')
        if not auth_header:
            return None
        
        parts = auth_header.split()
        if parts[0].lower() != 'bearer':
            return None
            
        if len(parts) == 1:
            raise exceptions.AuthenticationFailed('Invalid token header. No credentials provided.')
        elif len(parts) > 2:
            raise exceptions.AuthenticationFailed('Invalid token header. Token string should not contain spaces.')
            
        token = parts[1]
        
        try:
            payload = jwt.decode(
                token, 
                settings.JWT_PUBLIC_KEY, 
                algorithms=["RS256"], 
                options={"require": ["exp", "tenant_id"]}
            )
        except jwt.ExpiredSignatureError:
            raise exceptions.AuthenticationFailed('Token has expired.')
        except jwt.InvalidTokenError as e:
            raise exceptions.AuthenticationFailed(f'Invalid token: {e!s}')
            
        tenant_id_str = payload.get('tenant_id')
        try:
            tenant_id = uuid.UUID(tenant_id_str)
        except (ValueError, TypeError):
            raise exceptions.AuthenticationFailed('Invalid tenant_id format.')
            
        return (TenantUser(tenant_id), token)

    def authenticate_header(self, request):
        return 'Bearer'
