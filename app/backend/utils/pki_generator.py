"""PKI Certificate Generator for Testing and Admin Seeding"""
import os
import hashlib
from datetime import datetime, timedelta, timezone
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from loguru import logger


class PKICertificateGenerator:
    """Generate PKI certificates for testing and admin seeding"""
    
    @staticmethod
    def generate_admin_certificate():
        """Generate admin certificate with proper CN, Serial, and Fingerprint"""
        
        # Generate private key
        private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )
        
        # Certificate details
        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.COUNTRY_NAME, "US"),
            x509.NameAttribute(NameOID.STATE_OR_PROVINCE_NAME, "CA"),
            x509.NameAttribute(NameOID.LOCALITY_NAME, "San Francisco"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Radio Monitoring System"),
            x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, "Admin"),
            x509.NameAttribute(NameOID.COMMON_NAME, "admin@radiomonitoring.local"),
        ])
        
        # Create certificate
        cert = x509.CertificateBuilder().subject_name(
            subject
        ).issuer_name(
            issuer
        ).public_key(
            private_key.public_key()
        ).serial_number(
            1001  # Fixed serial number for admin
        ).not_valid_before(
            datetime.now(timezone.utc)
        ).not_valid_after(
            datetime.now(timezone.utc) + timedelta(days=3650)  # 10 years
        ).add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName("admin.radiomonitoring.local"),
                x509.RFC822Name("admin@radiomonitoring.local"),
            ]),
            critical=False,
        ).sign(private_key, hashes.SHA256())
        
        # Extract certificate details
        cn = "admin@radiomonitoring.local"
        serial = str(cert.serial_number)
        
        # Generate fingerprint (SHA256)
        fingerprint = hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()
        
        return {
            "cn": cn,
            "serial": serial,
            "fingerprint": fingerprint,
            "certificate": cert,
            "private_key": private_key
        }
    
    @staticmethod
    def generate_test_certificate(username: str, serial_number: int = None):
        """Generate test certificate for any user"""
        if serial_number is None:
            serial_number = hash(username) % (10**8)  # Generate unique serial from username
        
        # Generate private key
        private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )
        
        # Certificate details
        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.COUNTRY_NAME, "US"),
            x509.NameAttribute(NameOID.STATE_OR_PROVINCE_NAME, "CA"),
            x509.NameAttribute(NameOID.LOCALITY_NAME, "San Francisco"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Radio Monitoring System"),
            x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, "Users"),
            x509.NameAttribute(NameOID.COMMON_NAME, f"{username}@radiomonitoring.local"),
        ])
        
        # Create certificate
        cert = x509.CertificateBuilder().subject_name(
            subject
        ).issuer_name(
            issuer
        ).public_key(
            private_key.public_key()
        ).serial_number(
            serial_number
        ).not_valid_before(
            datetime.now(timezone.utc)
        ).not_valid_after(
            datetime.now(timezone.utc) + timedelta(days=365)
        ).add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName(f"{username}.radiomonitoring.local"),
                x509.RFC822Name(f"{username}@radiomonitoring.local"),
            ]),
            critical=False,
        ).sign(private_key, hashes.SHA256())
        
        # Extract certificate details
        cn = f"{username}@radiomonitoring.local"
        serial = str(cert.serial_number)
        
        # Generate fingerprint (SHA256)
        fingerprint = hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()
        
        return {
            "cn": cn,
            "serial": serial,
            "fingerprint": fingerprint,
            "certificate": cert,
            "private_key": private_key
        }
    
    @staticmethod
    def save_certificate_files(cert_data: dict, base_path: str, filename: str):
        """Save certificate and private key to files"""
        os.makedirs(base_path, exist_ok=True)
        
        # Save certificate
        cert_path = os.path.join(base_path, f"{filename}.crt")
        with open(cert_path, "wb") as f:
            f.write(cert_data["certificate"].public_bytes(serialization.Encoding.PEM))
        
        # Save private key
        key_path = os.path.join(base_path, f"{filename}.key")
        with open(key_path, "wb") as f:
            f.write(cert_data["private_key"].private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption()
            ))
        
        logger.info(f"Saved certificate files: {cert_path}, {key_path}")
        return cert_path, key_path


def generate_admin_pki_credentials():
    """Generate admin PKI credentials for configuration"""
    cert_data = PKICertificateGenerator.generate_admin_certificate()
    
    # Save to certificates directory
    PKICertificateGenerator.save_certificate_files(
        cert_data, 
        "certificates", 
        "admin"
    )
    
    return {
        "pki_certificate_cn": cert_data["cn"],
        "pki_certificate_serial": cert_data["serial"],
        "pki_certificate_fingerprint": cert_data["fingerprint"]
    }


if __name__ == "__main__":
    # Generate admin credentials when run directly
    credentials = generate_admin_pki_credentials()
    print("Generated Admin PKI Credentials:")
    for key, value in credentials.items():
        print(f"{key}: {value}")