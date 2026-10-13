import os
import re
from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from app.backend.enums.general import P12Encryption, RevocationReason
from app.backend.pki.authority import (
    CaCheckError, check_ca, is_signed_by, load_ca, load_ca_certificate, sha256_fingerprint,
)
from app.backend.pki.crl import CrlEntry, build_crl, earliest_next_update, split_pem_crls
from app.backend.pki.package import build_p12, generate_key, new_password, p12_filename
from app.backend.pki.profile import build_client_certificate, normalize_label
from app.backend.pki.store import (
    CertificateFiles, read_package, remove_certificate_dir, write_atomically, write_certificate_dir,
)
from tests.pki_helpers import client_ca, legacy_root, make_leaf, pem, write_ca_files
from tests.test_pki_login import CYRILLIC_CN

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)


def test_client_certificate_profile():
    ca_cert, ca_key = client_ca()
    cert = build_client_certificate(ca_cert, ca_key, generate_key(2048).public_key(), "Petrenko I. - post 2 - laptop", 365, NOW)
    assert cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == "Petrenko I. - post 2 - laptop"
    assert len(cert.subject) == 1 and cert.issuer == ca_cert.subject
    assert 64 < cert.serial_number.bit_length() <= 159
    assert cert.not_valid_before_utc == NOW - timedelta(minutes=5) and cert.not_valid_after_utc == NOW + timedelta(days=365)
    bc = cert.extensions.get_extension_for_class(x509.BasicConstraints)
    assert bc.critical and bc.value.ca is False
    ku = cert.extensions.get_extension_for_class(x509.KeyUsage)
    assert ku.critical and ku.value.digital_signature and not ku.value.key_cert_sign
    assert list(cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value) == [ExtendedKeyUsageOID.CLIENT_AUTH]
    with pytest.raises(x509.ExtensionNotFound):
        cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    ski = cert.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value
    aki = cert.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier).value
    ca_ski = ca_cert.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value
    assert ski.digest and aki.key_identifier == ca_ski.digest
    assert cert.signature_hash_algorithm.name == "sha256"
    cert.verify_directly_issued_by(ca_cert)


def test_cyrillic_label_is_the_cn():
    cert, _ = make_leaf(client_ca(), CYRILLIC_CN)
    assert cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == CYRILLIC_CN
    assert x509.load_pem_x509_certificate(pem(cert)).subject == cert.subject


def test_normalize_label():
    assert normalize_label("  Laptop ") == "Laptop" and normalize_label("a" * 64) == "a" * 64
    assert normalize_label("") is None and normalize_label("a" * 65) is None
    assert normalize_label("   ") is None
    assert normalize_label("a\tb") is None and normalize_label("a\u200eb") is None
    assert normalize_label(CYRILLIC_CN) == CYRILLIC_CN


def test_client_ca_profile():
    cert, key = client_ca()
    bc = cert.extensions.get_extension_for_class(x509.BasicConstraints)
    assert bc.critical and bc.value.ca is True and bc.value.path_length == 0
    ku = cert.extensions.get_extension_for_class(x509.KeyUsage)
    assert ku.critical and ku.value.key_cert_sign and ku.value.crl_sign
    assert list(cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value) == [ExtendedKeyUsageOID.CLIENT_AUTH]
    assert cert.issuer == cert.subject
    cert.verify_directly_issued_by(cert)
    check_ca(cert, key)


@pytest.mark.parametrize("encryption", list(P12Encryption))
def test_p12_opens_with_its_password(encryption):
    cert, key = make_leaf(client_ca())
    data = build_p12(key, cert, "Laptop", "pass-word", encryption)
    loaded = pkcs12.load_pkcs12(data, b"pass-word")
    assert loaded.cert.certificate == cert and loaded.cert.friendly_name == b"Laptop"
    assert loaded.additional_certs == []
    with pytest.raises(ValueError):
        pkcs12.load_pkcs12(data, b"wrong")


def test_password_format():
    passwords = {new_password() for _ in range(200)}
    assert len(passwords) == 200
    assert all(re.fullmatch(r"([2-9A-HJ-NP-Za-km-z]{5}-){3}[2-9A-HJ-NP-Za-km-z]{5}", p) for p in passwords)


def test_p12_filename():
    assert p12_filename("Petrenko I. - post 2 - laptop", 7) == "Petrenko-I-post-2-laptop.p12"
    assert p12_filename(CYRILLIC_CN, 7) == "certificate-7.p12"


def test_crl_entries_number_and_signature():
    ca_cert, ca_key = client_ca()
    entries = [
        CrlEntry(11, NOW - timedelta(days=1), RevocationReason.KEY_COMPROMISE),
        CrlEntry(12, NOW - timedelta(hours=1), RevocationReason.UNSPECIFIED),
    ]
    crl = build_crl(ca_cert, ca_key, 5, NOW, NOW + timedelta(days=7), entries)
    revoked = list(crl)
    assert [r.serial_number for r in revoked] == [11, 12]
    assert revoked[0].extensions.get_extension_for_class(x509.CRLReason).value.reason == x509.ReasonFlags.key_compromise
    with pytest.raises(x509.ExtensionNotFound):
        revoked[1].extensions.get_extension_for_class(x509.CRLReason)
    assert crl.extensions.get_extension_for_class(x509.CRLNumber).value.crl_number == 5
    aki = crl.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier).value
    assert aki.key_identifier == ca_cert.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value.digest
    assert crl.is_signature_valid(ca_cert.public_key())
    assert crl.signature_hash_algorithm.name == "sha256"


def test_earliest_next_update_of_a_bundle():
    ca_cert, ca_key = client_ca()
    first = build_crl(ca_cert, ca_key, 1, NOW, NOW + timedelta(days=7), [])
    second = build_crl(ca_cert, ca_key, 2, NOW, NOW + timedelta(days=3), [])
    data = pem(first) + pem(second)
    assert len(split_pem_crls(data)) == 2
    assert earliest_next_update(data) == NOW + timedelta(days=3)
    assert earliest_next_update(b"") is None


def _ca_without_crl_sign():
    key = generate_key(2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "no crl sign")])
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(NOW).not_valid_after(NOW + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=False, content_commitment=False, key_encipherment=False, data_encipherment=False,
            key_agreement=False, key_cert_sign=True, crl_sign=False, encipher_only=False, decipher_only=False,
        ), critical=True)
        .sign(key, hashes.SHA256())
    )
    return cert, key


def test_check_ca_refusals(tmp_path):
    ca = client_ca()
    leaf_cert, leaf_key = make_leaf(ca)
    with pytest.raises(CaCheckError) as err:
        check_ca(leaf_cert)
    assert err.value.reason == "not_a_ca"
    with pytest.raises(CaCheckError) as err:
        check_ca(ca[0], leaf_key)
    assert err.value.reason == "key_mismatch"

    no_crl_sign, no_crl_key = _ca_without_crl_sign()
    with pytest.raises(CaCheckError) as err:
        check_ca(no_crl_sign, no_crl_key)
    assert err.value.reason == "key_usage"

    cert_path, key_path = write_ca_files(tmp_path, ca, "secret")
    for password in ("", "wrong"):
        with pytest.raises(CaCheckError) as err:
            load_ca(str(cert_path), str(key_path), password)
        assert err.value.reason == "key_password"
    cert, key = load_ca(str(cert_path), str(key_path), "secret")
    assert cert == ca[0] and key.public_key().public_numbers() == ca[1].public_key().public_numbers()

    bad = tmp_path / "bad.crt"
    bad.write_bytes(b"not a certificate")
    with pytest.raises(CaCheckError) as err:
        load_ca_certificate(str(bad))
    assert err.value.reason == "unreadable"
    with pytest.raises(CaCheckError) as err:
        load_ca_certificate(str(tmp_path / "missing.crt"))
    assert err.value.reason == "unreadable"
    assert load_ca_certificate(str(cert_path)) == ca[0]


def test_is_signed_by():
    leaf, _ = make_leaf(client_ca())
    assert is_signed_by(leaf, client_ca()[0])
    assert not is_signed_by(leaf, legacy_root()[0])
    other_leaf, _ = make_leaf(legacy_root())
    assert not is_signed_by(other_leaf, client_ca()[0])
    assert re.fullmatch(r"([0-9A-F]{2}:){31}[0-9A-F]{2}", sha256_fingerprint(leaf))


def _files():
    return CertificateFiles(b"CERT", b"P12DATA", "pass-word", "info text")


def test_write_certificate_dir(tmp_path):
    assert write_certificate_dir(tmp_path, 3, 9, _files()) == "3/9"
    folder = tmp_path / "3" / "9"
    assert sorted(p.name for p in folder.iterdir()) == ["certificate.crt", "certificate.p12", "info.txt", "password.txt"]
    assert read_package(tmp_path, "3/9") == (b"P12DATA", "pass-word")
    with pytest.raises(FileExistsError):
        write_certificate_dir(tmp_path, 3, 9, _files())
    assert not [p for p in (tmp_path / "3").iterdir() if p.name.startswith(".tmp-")]
    remove_certificate_dir(tmp_path, "3/9")
    remove_certificate_dir(tmp_path, "3/9")
    with pytest.raises(FileNotFoundError):
        read_package(tmp_path, "3/9")


def test_secrets_never_appear_in_a_repr():
    """The .p12 and its password stay out of a repr, so out of a log line or an exception text"""
    from app.backend.models.user_certificate import UserCertificate
    from app.backend.services.certificates import CertificatePackage

    package = CertificatePackage(certificate=UserCertificate(id=9, label="Laptop"), p12=b"P12DATA",
                                 password="pass-word", filename="Laptop-9.p12")
    for value in (_files(), package):
        text = repr(value)
        assert "P12DATA" not in text and "pass-word" not in text, text
    assert "Laptop-9.p12" in repr(package) and "info text" in repr(_files())


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_certificate_dir_permissions(tmp_path):
    tmp_path.chmod(0o770)   # the store as deployed: root, group PKI_SOCKET_GID (PKI.md, section 3)
    write_certificate_dir(tmp_path, 3, 9, _files())
    folder = tmp_path / "3" / "9"
    assert (tmp_path / "3").stat().st_mode & 0o777 == 0o700
    assert folder.stat().st_mode & 0o777 == 0o700
    assert (folder / "certificate.p12").stat().st_mode & 0o777 == 0o600
    assert (folder / "password.txt").stat().st_mode & 0o777 == 0o600
    assert (folder / "certificate.crt").stat().st_mode & 0o777 == 0o644


def test_write_atomically_replaces(tmp_path):
    path = tmp_path / "ca.crl"
    write_atomically(path, b"one")
    write_atomically(path, b"two")
    assert path.read_bytes() == b"two"
    assert [p.name for p in tmp_path.iterdir()] == ["ca.crl"]
