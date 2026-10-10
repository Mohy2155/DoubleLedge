import os
import uuid
import datetime
import jwt
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from ledger.models import Tenant


def get_or_create_keypair():
    scratch_dir = settings.BASE_DIR / 'scratch'
    priv_path = scratch_dir / 'priv.pem'
    pub_path = scratch_dir / 'pub.pem'

    if priv_path.exists() and pub_path.exists():
        return priv_path.read_text(), pub_path.read_text()

    scratch_dir.mkdir(parents=True, exist_ok=True)

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption()
    ).decode('utf-8')
    pub_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode('utf-8')

    priv_path.write_text(priv_pem)
    pub_path.write_text(pub_pem)
    return priv_pem, pub_pem


class Command(BaseCommand):
    help = 'Creates a new tenant and generates a dev JWT, or mints a token for an existing tenant.'

    def add_arguments(self, parser):
        parser.add_argument('--name', type=str, default='Demo Tenant', help='Name of the tenant')
        parser.add_argument('--hours', type=int, default=24, help='Token lifetime in hours (default: 24)')
        parser.add_argument('--tenant_id', type=str, default=None, help='Existing tenant ID to mint token for')

    def handle(self, *args, **options):
        name = options['name']
        hours = options['hours']
        existing_tenant_id = options.get('tenant_id')

        if existing_tenant_id:
            try:
                target_uuid = uuid.UUID(existing_tenant_id)
            except ValueError:
                raise CommandError(f"Invalid UUID: {existing_tenant_id}")

            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SELECT set_config('app.tenant_id', %s, true)", [str(target_uuid)])
                try:
                    tenant = Tenant.objects.get(id=target_uuid)
                except Tenant.DoesNotExist:
                    raise CommandError(f"Tenant {target_uuid} does not exist.")

            priv_pem, _ = get_or_create_keypair()
            exp = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=hours)
            token = jwt.encode(
                {
                    'tenant_id': str(tenant.id),
                    'exp': int(exp.timestamp())
                },
                priv_pem,
                algorithm='RS256'
            )

            self.stdout.write(self.style.SUCCESS(f"Token generated successfully for existing tenant!"))
            self.stdout.write(f"TENANT_ID: {tenant.id}")
            self.stdout.write(f"TOKEN: {token}")
            return

        tenant_id = uuid.uuid4()

        # Insert tenant under owner role with app.tenant_id set
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SELECT set_config('app.tenant_id', %s, true)", [str(tenant_id)])
            tenant = Tenant.objects.create(id=tenant_id, name=name)

        priv_pem, _ = get_or_create_keypair()
        exp = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=hours)
        token = jwt.encode(
            {
                'tenant_id': str(tenant.id),
                'exp': int(exp.timestamp())
            },
            priv_pem,
            algorithm='RS256'
        )

        self.stdout.write(self.style.SUCCESS(f"Tenant created successfully!"))
        self.stdout.write(f"TENANT_ID: {tenant.id}")
        self.stdout.write(f"TOKEN: {token}")
