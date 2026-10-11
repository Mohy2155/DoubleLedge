import sys
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization

def main():
    scratch_dir = Path('/app/scratch')
    priv_path = scratch_dir / 'priv.pem'
    pub_path = scratch_dir / 'pub.pem'

    if priv_path.exists() and pub_path.exists():
        print("keys exist")
        return

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
    print("keys generated")

if __name__ == '__main__':
    main()
