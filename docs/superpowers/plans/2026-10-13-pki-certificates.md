# PKI Client Certificates Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The app issues, lists, revokes and re-downloads the client certificates people log in with, from a new
client CA whose key lives only in a networkless signer container, and publishes a CRL that nginx reloads.

**Architecture:** A pure library (`app/backend/pki/`) builds certificates, `.p12` packages and CRLs with
`cryptography`. A small stateless FastAPI service (`app/backend/signer/`, container `pki-signer`, Unix socket only)
holds the client CA key and signs. The API keeps the single source of truth: certificates move from three hash
columns on `users` to a `user_certificates` table (migration 024, existing certificates become `legacy` rows), the
API generates keys, asks the signer to sign, writes the certificate folder, and publishes the CRL after every
revocation and daily.

**Tech Stack:** Python 3, FastAPI, SQLModel / SQLAlchemy async, Alembic, pydantic v2 + pydantic-settings 2.10,
`cryptography` 45.0.7, httpx 0.28 (Unix socket transport), uvicorn `--uds`, pytest (in-memory sqlite), MySQL 8 /
MariaDB 11 in production, nginx 1.27 on the host, systemd units.

**Spec:** `docs/superpowers/specs/2026-10-12-pki-certificates-design.md` (read it before any task; "spec section N"
below refers to it).

## Global Constraints

- Branch `pki-certificates`, created from `main` before Task 1. Every commit message ends with
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Stage only the files of the task by path. Never `git add -A` or `git add .`: the working tree holds unrelated
  files of the user (`create_cert.sh`, `gencert.sh`, a change in `app/backend/utils/logging_config.py`) that must
  not be committed.
- ASCII only in code, comments, docs and commit messages; no emoji. Ukrainian text only in `PERMISSION_META` display
  names, written through Edit/Write (never through the shell: mojibake).
- Read `CLAUDE.md` first. Every path is a member of its group in `app/backend/enums/routes.py` before it is used; no
  `"/api/v1/..."` or `"/"` literal anywhere, tests included (`V1.path(XRoutes.MEMBER, ...)`). Literal paths
  (`/upload`) are registered before `/{certificate_id}`.
- Errors: `ApiError(ErrorCode.X, **params)`, `access_denied(AccessErrorCode.X, ...)`, `invalid(FieldErrorCode.X)` in
  request validators, `CodedError` in services and `ApiError.from_coded(exc)` in routes; every raised code is in the
  route's `responses=problems(...)`; every new `ProblemType` member is declared by at least one route in the task
  that adds it (`tests/test_openapi.py::test_every_problem_type_is_documented_somewhere`).
- Pydantic v2 only (`field_validator`, `model_validator`); never `@validator`.
- `services/`, `filters/`, `models/`, `bootstrap/`, `utils/`, `enums/` and the new `pki/` never import FastAPI,
  Starlette or `app.backend.api`. `pki/` and `signer/` never import `app.backend.config` (the signer runs without the
  app's settings, which require a database password and a secret key).
- Migrations: tables, columns and indexes guarded with inspector checks (`sa.inspect(op.get_bind())`, style of
  `023_authorization_v2.py`: `_log`, `_tables()`); new tables listed in `TABLES_OWNED_BY_LATER_REVISIONS`
  (`002_sync_schema_with_models.py`); a working, exact downgrade.
- Values pinned by the spec: label 1..64 characters; user key RSA 2048 (`key_size`), client CA RSA 4096, 10 years;
  validity default 365, max 730; backdate 5 minutes; CRL `crl_days` 7; signer `max_crl_days` 31; password 20
  characters in groups of 5 joined by `-`, alphabet without `0 O 1 l I`; health degraded below 2 days.
- Tests: `venv\Scripts\python -m pytest -q` (whole suite) passes before every commit. POSIX permission assertions
  use `@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")`. Throwaway CAs only, never a real key.
- Every task that changes the API or the database adds its lines to `CHANGELOG.md` under a new heading
  `## PKI client certificates` (Added / Changed / Removed) and, when the API changed, regenerates the contract on
  purpose: `venv\Scripts\python -m app.backend.cli.api_contract`, then `tests/test_api_contract.py` passes.
- No hard delete: certificates are never deleted, archived or restored; revoking is final.

## Review Focus

Inputs the spec implies that no task would otherwise test; each line has its test in the owning task.

1. A certificate issued with a Cyrillic label logs in through the nginx headers (`$ssl_client_s_dn` escapes every
   byte above 0x7F as `\XX`) -> 200, same user (Task 6, `test_cyrillic_label_logs_in_through_nginx`).
2. Downgrade of 024 when two users' newest certificates share a CN hash (both labelled `laptop`) -> the first keeps
   it, the second gets the `unassigned:<user_id>` placeholder, the downgrade logs that user (Task 4,
   `test_downgrade_placeholder_for_a_taken_cn_hash`).
3. The existing root's CRL in `extra_crl_paths` expires before the client CA's CRL -> health reports the earlier
   `next_update` and `degraded` (Task 5, `test_health_reports_the_earliest_next_update_of_an_extra_crl`).
4. Issuing fails after the folder is written (the audit write raises) -> no row, no directory, no `.tmp-` leftover
   (Task 6, `test_failed_issue_leaves_no_directory`).
5. A revocation while the signer is down -> 200, CRL stale in health; the next publish (signer back) lists it and
   clears the stale flag (Task 7, `test_revocation_while_the_signer_is_down_reaches_the_next_crl`).

---

## File Map

| File | Responsibility | Task |
|------|----------------|------|
| `app/backend/pki/profile.py` | label rule, client certificate and client CA builders | 1 |
| `app/backend/pki/authority.py` | CA checks, loading, `is_signed_by`, fingerprint | 1 |
| `app/backend/pki/package.py` | user key, `.p12`, password, filename | 1 |
| `app/backend/pki/crl.py` | CRL builder, PEM bundle parsing | 1 |
| `app/backend/pki/store.py` | certificate folder, atomic file writes | 1 |
| `tests/pki_helpers.py` | throwaway CAs and leaves for every PKI test | 1 |
| `app/backend/signer/{settings,models,app,__main__}.py` | the signing service and its CLI | 2 |
| `app/backend/models/user_certificate.py` | `UserCertificate`, `CertificateAuthorityState` | 4 |
| `app/backend/migrations/versions/024_user_certificates.py` | tables, legacy rows, drop `users.pki_certificate_*` | 4 |
| `app/backend/services/certificates.py` | state, registration, issue, export, revoke (dataclasses, `CodedError`) | 4, 6, 7 |
| `app/backend/services/signer_client.py` | httpx client of the signer | 5 |
| `app/backend/services/crl_publisher.py` | CRL number, entries, file, status | 5 |
| `app/backend/bootstrap/pki.py` | settings check, CRL at startup | 5 |
| `app/backend/cli/pki.py` | `issue`, `crl`, `status` | 5, 6 |
| `app/backend/api/v1/certificates.py` | `/certificates/`, `/certificates/authority` | 5, 6 |
| `app/backend/api/v1/user_certificates.py` | `/users/{user_id}/certificates...` | 6, 7 |
| `app/backend/api/v1/models/{request,response,query}/certificates.py` | contract of both modules | 5, 6, 7 |
| `app/backend/filters/certificate_filter.py` | `CertificateCriteria`, `CertificateFilterService` | 6 |
| `docker-compose.yml`, `nginx.conf`, `signer.env.example`, `.env.example`, `deploy/systemd/*`, `deploy/pki/remove-old-root.cmd` | deployment | 8 |
| `PKI.md`, `CLAUDE.md`, `API.md`, `DEPLOYMENT.md`, `CHANGELOG.md` | documents | 9 |

---

### Task 1: PKI library

**Files:**
- Create: `app/backend/pki/__init__.py` (docstring only), `app/backend/pki/profile.py`, `app/backend/pki/authority.py`,
  `app/backend/pki/package.py`, `app/backend/pki/crl.py`, `app/backend/pki/store.py`, `tests/pki_helpers.py`
- Modify: `app/backend/enums/general.py` (`RevocationReason`, `P12Encryption`), `tests/test_route_registry.py`
  (`FRAMEWORK_FREE` += `"pki"`)
- Test: `tests/test_pki.py`

**Interfaces:**
- Produces:
  - `enums/general.py`: `class RevocationReason(str, Enum)`: `UNSPECIFIED="unspecified"`, `KEY_COMPROMISE="key_compromise"`,
    `AFFILIATION_CHANGED="affiliation_changed"`, `SUPERSEDED="superseded"`, `CESSATION_OF_OPERATION="cessation_of_operation"`;
    `class P12Encryption(str, Enum)`: `AES256="aes256"`, `LEGACY="legacy"`.
  - `pki/profile.py`: `LABEL_MAX_LENGTH = 64`, `BACKDATE = timedelta(minutes=5)`, `CLIENT_CA_KEY_SIZE = 4096`;
    `def normalize_label(value: str) -> Optional[str]` (stripped value, or None when empty, longer than 64, or holding
    a character of Unicode category `C*`);
    `def build_client_certificate(ca_cert: x509.Certificate, ca_key: rsa.RSAPrivateKey, public_key: rsa.RSAPublicKey, label: str, validity_days: int, now: Optional[datetime] = None) -> x509.Certificate`
    (profile of spec section 5; `x509.random_serial_number()`; `not_before = now - BACKDATE`, `not_after = now + validity_days`);
    `def build_client_ca(common_name: str = "watch_web client CA", days: int = 3650, key_size: int = CLIENT_CA_KEY_SIZE, now: Optional[datetime] = None) -> tuple[x509.Certificate, rsa.RSAPrivateKey]`
    (spec section 1: `CA:TRUE, pathlen:0` critical, `keyCertSign, cRLSign` critical, EKU `clientAuth`, SKI).
  - `pki/authority.py`: `class CaCheckError(Exception)` with attribute `reason: str` in
    `{"unreadable", "not_a_ca", "key_mismatch", "key_usage", "key_password"}`;
    `def check_ca(cert: x509.Certificate, key: Optional[rsa.RSAPrivateKey] = None) -> None`;
    `def load_ca_certificate(path: str) -> x509.Certificate` (PEM, then `check_ca`);
    `def load_ca(cert_path: str, key_path: str, password: str = "") -> tuple[x509.Certificate, rsa.RSAPrivateKey]`;
    `def is_signed_by(cert: x509.Certificate, ca_cert: x509.Certificate) -> bool` (issuer equals subject and
    `verify_directly_issued_by` passes; False on any `ValueError`, `TypeError`, `InvalidSignature`);
    `def sha256_fingerprint(cert: x509.Certificate) -> str` (upper-case hex pairs joined by `:`).
  - `pki/package.py`: `PASSWORD_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"`;
    `def new_password() -> str` (`secrets.choice`, 4 groups of 5, `-` between);
    `def generate_key(key_size: int) -> rsa.RSAPrivateKey` (exponent 65537);
    `def build_p12(key: rsa.RSAPrivateKey, cert: x509.Certificate, label: str, password: str, encryption: P12Encryption) -> bytes`
    (`pkcs12.serialize_key_and_certificates(label.encode("utf-8"), key, cert, None, ...)`; AES256:
    `PrivateFormat.PKCS12.encryption_builder().kdf_rounds(50000).key_cert_algorithm(pkcs12.PBES.PBESv2SHA256AndAES256CBC).hmac_hash(hashes.SHA256()).build(...)`;
    LEGACY: `PBESv1SHA1And3KeyTripleDESCBC` with `hashes.SHA1()`; no CA certificate inside);
    `def p12_filename(label: str, certificate_id: int) -> str` (ASCII letters and digits kept, every run of other
    characters becomes one `-`, `-` trimmed at both ends; empty -> `certificate-<id>`; plus `.p12`).
  - `pki/crl.py`: `@dataclass(frozen=True) class CrlEntry: serial: int; revoked_at: datetime; reason: RevocationReason`;
    `def build_crl(ca_cert, ca_key, number: int, this_update: datetime, next_update: datetime, entries: Sequence[CrlEntry]) -> x509.CertificateRevocationList`
    (extensions `CRLNumber(number)` and `AuthorityKeyIdentifier` from the CA's SKI; `CRLReason` only for a reason
    other than `unspecified`; SHA-256);
    `def split_pem_crls(data: bytes) -> list[x509.CertificateRevocationList]` (cryptography 45 has no bulk loader:
    split on `-----BEGIN X509 CRL-----` blocks, `x509.load_pem_x509_crl` each);
    `def earliest_next_update(data: bytes) -> Optional[datetime]` (min `next_update_utc` of the bundle).
  - `pki/store.py`: `@dataclass(frozen=True) class CertificateFiles: certificate_pem: bytes; p12: bytes; password: str; info: str`;
    `def write_certificate_dir(store_dir: Path, user_id: int, certificate_id: int, files: CertificateFiles) -> str`
    (returns `"<user_id>/<certificate_id>"`; writes into `<store_dir>/<user_id>/.tmp-<certificate_id>-<random>`, then
    `os.rename`; `FileExistsError` when the target exists; directories 0700, `certificate.p12` and `password.txt` 0600,
    `certificate.crt` and `info.txt` 0644; removes the temp directory on failure and re-raises);
    `def remove_certificate_dir(store_dir: Path, files_dir: str) -> None` (missing is fine);
    `def read_package(store_dir: Path, files_dir: str) -> tuple[bytes, str]` (`.p12` bytes, password; `FileNotFoundError`);
    `def write_atomically(path: Path, data: bytes, mode: int = 0o644) -> None` (`<path>.tmp` in the same directory, then `os.replace`).
  - `tests/pki_helpers.py`: `def client_ca() -> tuple[x509.Certificate, rsa.RSAPrivateKey]` (cached,
    `build_client_ca(key_size=2048)`); `def legacy_root() -> tuple[...]` (cached self-signed root without extensions,
    like `gencert.sh`); `def make_leaf(ca: tuple, label: str = "Test device", days: int = 30, now: Optional[datetime] = None) -> tuple[x509.Certificate, rsa.RSAPrivateKey]`;
    `def pem(obj) -> bytes`; `def write_ca_files(directory: Path, ca: tuple, password: str = "") -> tuple[Path, Path]`.

- [ ] **Step 1: Create the branch**

Run: `git switch -c pki-certificates`
Expected: `Switched to a new branch 'pki-certificates'`

- [ ] **Step 2: Write the failing tests** in `tests/test_pki.py`:

```python
def test_client_certificate_profile():
    ca_cert, ca_key = client_ca()
    now = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
    cert = build_client_certificate(ca_cert, ca_key, generate_key(2048).public_key(), "Petrenko I. - post 2 - laptop", 365, now)
    assert cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == "Petrenko I. - post 2 - laptop"
    assert len(cert.subject) == 1 and cert.issuer == ca_cert.subject
    assert 64 < cert.serial_number.bit_length() <= 159
    assert cert.not_valid_before_utc == now - timedelta(minutes=5) and cert.not_valid_after_utc == now + timedelta(days=365)
    bc = cert.extensions.get_extension_for_class(x509.BasicConstraints)
    assert bc.critical and bc.value.ca is False
    ku = cert.extensions.get_extension_for_class(x509.KeyUsage)
    assert ku.critical and ku.value.digital_signature and not ku.value.key_cert_sign
    assert list(cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value) == [ExtendedKeyUsageOID.CLIENT_AUTH]
    with pytest.raises(x509.ExtensionNotFound):
        cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    assert cert.signature_hash_algorithm.name == "sha256"
    cert.verify_directly_issued_by(ca_cert)

def test_cyrillic_label_is_the_cn(): ...          # CYRILLIC label round-trips as the CN value
def test_normalize_label():
    assert normalize_label("  Laptop ") == "Laptop" and normalize_label("a" * 64) == "a" * 64
    assert normalize_label("") is None and normalize_label("a" * 65) is None
    assert normalize_label("a\tb") is None and normalize_label("a\u200eb") is None

def test_client_ca_profile(): ...                 # CA:TRUE pathlen 0 critical, keyCertSign+cRLSign critical, EKU clientAuth, self-signed, check_ca passes

@pytest.mark.parametrize("encryption", list(P12Encryption))
def test_p12_opens_with_its_password(encryption):
    key, cert = ...
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

def test_crl_entries_number_and_signature(): ...  # two entries (key_compromise, unspecified): serials, CRLReason only on the first, CRLNumber == 5, AKI == CA SKI, is_signature_valid(ca public key)
def test_earliest_next_update_of_a_bundle(): ... # two CRLs concatenated: split_pem_crls gives 2, earliest_next_update the smaller
def test_check_ca_refusals(tmp_path): ...       # leaf as CA -> not_a_ca; other key -> key_mismatch; CA keyUsage without cRLSign -> key_usage; encrypted key, no or wrong password -> key_password
def test_is_signed_by(): ...                    # leaf of client_ca True, leaf of legacy_root False
def test_write_certificate_dir(tmp_path): ...   # files present, read_package returns (p12, password), second write same ids -> FileExistsError, no ".tmp-" left
@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_certificate_dir_permissions(tmp_path): ...  # dir 0o700, certificate.p12 and password.txt 0o600
def test_write_atomically_replaces(tmp_path): ...
```

- [ ] **Step 3: Run them to verify they fail**

Run: `venv\Scripts\python -m pytest tests/test_pki.py -q`
Expected: FAIL (`ModuleNotFoundError: app.backend.pki`)

- [ ] **Step 4: Implement** the five modules, the two enums and the helpers with the interfaces above.

- [ ] **Step 5: Run the tests and the whole suite**

Run: `venv\Scripts\python -m pytest tests/test_pki.py tests/test_route_registry.py -q` then `venv\Scripts\python -m pytest -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add app/backend/pki tests/pki_helpers.py tests/test_pki.py app/backend/enums/general.py tests/test_route_registry.py
git commit -m "PKI library: client certificate and CA profile, p12, password, CRL, certificate folder"
```

---

### Task 2: Signing service

**Files:**
- Create: `app/backend/signer/__init__.py`, `app/backend/signer/settings.py`, `app/backend/signer/models.py`,
  `app/backend/signer/app.py`, `app/backend/signer/__main__.py`
- Modify: `app/backend/enums/routes.py` (`SignerRoutes`, without `BASE`, not in `class Routes`)
- Test: `tests/test_signer.py`

**Interfaces:**
- Consumes: Task 1 `pki.profile`, `pki.authority`, `pki.crl`, `RevocationReason`.
- Produces:
  - `class SignerRoutes(str, Enum)`: `SIGN_CLIENT = "/sign/client"`, `SIGN_CRL = "/sign/crl"`, `HEALTH = "/health"`
    (no `BASE` member: the route-registry tests check only groups with `BASE` against the main app; the signer's
    own test checks its routes).
  - `class SignerSettings(BaseSettings)`, `model_config = SettingsConfigDict(env_prefix="SIGNER_")`: `ca_cert_path: str`,
    `ca_key_path: str`, `ca_key_password: str = ""`, `socket_path: str = ""`, `max_validity_days: int = 730`,
    `max_crl_days: int = 31`.
  - `signer/models.py`: `SignClientRequest(public_key_pem: str, label: str, validity_days: int)`,
    `SignClientResponse(certificate_pem: str, serial: str)` (serial lower-case hex), `CrlEntryIn(serial: str, revoked_at: datetime, reason: RevocationReason)`,
    `SignCrlRequest(crl_number: int, this_update: datetime, next_update: datetime, entries: list[CrlEntryIn])`,
    `SignCrlResponse(crl_pem: str)`, `SignerHealthResponse(subject: str, not_after: datetime, fingerprint_sha256: str)`.
  - `signer/app.py`: `class SigningRefused(Exception)` with `reason: str` in
    `{"public_key", "label", "validity_days", "crl_number", "crl_dates"}`;
    `def create_app(settings: SignerSettings) -> FastAPI` (loads the CA with `load_ca` at creation, so a bad CA raises
    `CaCheckError` before serving; one exception handler answers `SigningRefused` as 422
    `JSONResponse(media_type="application/problem+json", content={"type": "about:blank", "title": "Signing refused", "status": 422, "reason": ...})`;
    `openapi_url=None`, `docs_url=None`, `redoc_url=None`).
    Checks: RSA public key of 2048..4096 bits; `normalize_label(label) == label`; `1 <= validity_days <= max_validity_days`;
    `crl_number >= 1`; `this_update < next_update <= now + max_crl_days`. Every signature logs one loguru line
    (`signed certificate serial=... cn=... not_after=... sha256=...` / `signed crl number=... entries=...`).
  - `signer/__main__.py`: `def main(argv: Optional[list[str]] = None) -> int` with subcommands
    `serve` (logging `logger.remove(); logger.add(sys.stderr, diagnose=False)`; removes a stale socket file;
    `os.umask(0o117)` so the socket is 0660; `uvicorn.run(create_app(settings), uds=settings.socket_path)`; prints the
    `CaCheckError.reason` and returns 1 on a bad CA),
    `init [--cn CN] [--days N]` (writes `build_client_ca` to `SIGNER_CA_CERT_PATH` 0644 and `SIGNER_CA_KEY_PATH` 0400,
    key encrypted with `BestAvailableEncryption` when `SIGNER_CA_KEY_PASSWORD` is set; returns 1 without writing when
    either file exists),
    `sign-crl --ca-cert F --ca-key F [--key-password-file F] --next-update YYYY-MM-DD --out F [--number N] [--serial HEX ...]`
    (any CA, for the existing root; `--number` default 1; reason `unspecified`, `revoked_at` now).

- [ ] **Step 1: Write the failing tests** in `tests/test_signer.py` (fixture `signer(tmp_path)`: `write_ca_files`
  of `client_ca()`, `create_app(SignerSettings(...))`, an `httpx.AsyncClient(transport=httpx.ASGITransport(app=...), base_url="http://signer")`):

```python
async def test_sign_client_uses_the_fixed_profile(signer):
    body = {"public_key_pem": public_pem(2048), "label": "Laptop", "validity_days": 30,
            "extensions": [{"oid": "2.5.29.19", "ca": True}], "issuer": "CN=Evil", "subject": "CN=bank.example"}
    r = await signer.post(SignerRoutes.SIGN_CLIENT.value, json=body)
    assert r.status_code == 200
    cert = x509.load_pem_x509_certificate(r.json()["certificate_pem"].encode())
    assert cert.issuer == client_ca()[0].subject and r.json()["serial"] == format(cert.serial_number, "x")
    assert cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is False
    assert list(cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value) == [ExtendedKeyUsageOID.CLIENT_AUTH]

@pytest.mark.parametrize("change, reason", [
    ({"public_key_pem": ec_public_pem()}, "public_key"), ({"public_key_pem": public_pem(1024)}, "public_key"),
    ({"label": ""}, "label"), ({"label": "a" * 65}, "label"), ({"label": " padded"}, "label"),
    ({"validity_days": 0}, "validity_days"), ({"validity_days": 731}, "validity_days")])
async def test_client_refusals(signer, change, reason):
    r = await signer.post(SignerRoutes.SIGN_CLIENT.value, json={**valid_body(), **change})
    assert r.status_code == 422 and r.json()["reason"] == reason

async def test_crl_refusals(signer): ...         # crl_number 0 -> crl_number; next_update <= this_update or > now+31d -> crl_dates
async def test_sign_crl(signer): ...             # signature valid against the CA, CRLNumber, one entry with its reason
async def test_health(signer): ...               # subject, not_after, fingerprint_sha256 of the CA
def test_refuses_to_start_on_a_bad_ca(tmp_path): ...   # not_a_ca, key_mismatch, key_password (encrypted key, password missing and wrong)
def test_every_route_is_an_enum_member(): ...    # {r.path for r in app.routes if isinstance(r, APIRoute)} == {m.value for m in SignerRoutes}
def test_signer_never_imports_app_internals(): ...  # transitive app.* imports of app/backend/signer (tests.test_route_registry._imported_modules) stay within app.backend.{signer,pki,enums}
async def test_every_signature_is_logged(signer): ...  # a loguru sink sees one "signed certificate serial=" line per request
def test_cli_init_writes_once(tmp_path, monkeypatch): ...  # main(["init"]) -> 0, files written; again -> 1, files unchanged
def test_cli_sign_crl_for_another_ca(tmp_path): ...    # legacy_root files, --serial 0a1b -> CRL signed by legacy_root listing 0x0a1b, next_update as given
```

- [ ] **Step 2: Run them to verify they fail**

Run: `venv\Scripts\python -m pytest tests/test_signer.py -q`
Expected: FAIL (`ModuleNotFoundError: app.backend.signer`)

- [ ] **Step 3: Implement** the settings, models, app and CLI with the interfaces above.

- [ ] **Step 4: Run the tests and the whole suite**

Run: `venv\Scripts\python -m pytest tests/test_signer.py tests/test_route_registry.py tests/test_error_contract.py -q` then `venv\Scripts\python -m pytest -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/backend/signer app/backend/enums/routes.py tests/test_signer.py
git commit -m "Signing service: client certificates and CRLs over a Unix socket, init and sign-crl commands"
```

---

### Task 3: Browser gate (live check 1)

The two-chain design depends on browsers offering a client certificate whose CA the machine does not trust.
Nothing after this task is built until it passes. No repository change; everything lives in the session scratchpad.

**Files:** none in the repository (scratch: a script and an nginx config in the scratchpad directory).

- [ ] **Step 1: Prepare** with a scratch script using Task 1: a client CA, two `.p12` files from it (labels
  `Gate test - laptop` and the Cyrillic `CYRILLIC_CN` of `tests/test_pki_login.py`), a server certificate for
  `localhost` from a separate throwaway CA (a browser warning on the server certificate is expected and accepted),
  and an nginx config: `listen 443 ssl`, `ssl_verify_client on`, `ssl_client_certificate` = the client CA only,
  `ssl_verify_depth 1`, `location / { return 200 "$ssl_client_verify $ssl_client_s_dn\n"; }`.
  Run `nginx:1.27` with it on port 18443 (docker usage as in memory `nginx-live-check.md`; `MSYS_NO_PATHCONV=1`).

- [ ] **Step 2: Verify the server side**

Run: `curl -k --cert-type P12 --cert gate-laptop.p12:<password> https://localhost:18443/`
Expected: `SUCCESS CN=Gate test - laptop`; without `--cert`: HTTP 400.

- [ ] **Step 3: Ask the user** (the controller, not a subagent) to import both `.p12` files (Windows: double-click,
  "Current User", "Automatically select the certificate store"; do not install any CA) and open
  `https://localhost:18443/` in Chrome, Edge and Firefox (Firefox: Settings > Certificates > Your Certificates >
  Import), and report for each browser: the picker offered the certificates, the labels were readable, the page
  showed `SUCCESS`.
Expected: all three browsers pass. If any fails, stop and report to the user: the trust design (spec section 1)
needs revisiting before Task 4.

- [ ] **Step 4: Clean up** the container; record the result in the task report (no commit).

---

### Task 4: Data switch to `user_certificates`

**Files:**
- Create: `app/backend/models/user_certificate.py`, `app/backend/migrations/versions/024_user_certificates.py`,
  `app/backend/services/certificates.py`, `tests/test_migration_024_certificates.py`, `tests/test_certificate_rows.py`
- Modify: `app/backend/models/user.py` (drop the four `pki_certificate_*` fields), `app/backend/models/__init__.py`,
  `app/backend/migrations/versions/002_sync_schema_with_models.py` (`TABLES_OWNED_BY_LATER_REVISIONS` +=
  `"user_certificates"`, `"certificate_authority_state"`), `app/backend/enums/general.py` (`CertificateSource`,
  `CertificateState`, `SessionRevokeCause.CERTIFICATE_REVOKED`), `app/backend/enums/errors.py`
  (`AuthErrorCode.CERTIFICATE_REVOKED`, `FieldErrorCode.CERTIFICATE_LABEL_INVALID`, `CERTIFICATE_TAKEN` title),
  `app/backend/config.py` (`PkiConfig`, `Settings.pki`), `app/backend/auth/certificates.py`, `app/backend/auth/service.py`,
  `app/backend/auth/dependencies.py`, `app/backend/api/v1/auth.py`, `app/backend/api/v1/users.py`,
  `app/backend/api/v1/models/request/users.py`, `app/backend/bootstrap/seed.py`, `app/backend/cli/init_user.py`,
  `tests/helpers.py` (`_new_user` without hashes; `make_certificate_row`), `tests/test_pki_login.py`,
  `tests/test_cn_hash_scheme.py`, `tests/test_migrations.py` (`HEAD = "024_user_certificates"`, table set),
  every other test that reads `pki_certificate_*`, `CHANGELOG.md`, `api-contract/v1/openapi.json`
- Test: `tests/test_certificate_rows.py`, `tests/test_migration_024_certificates.py`, the updated login tests

**Interfaces:**
- Consumes: Task 1 `pki.authority.load_ca_certificate`, `is_signed_by`, `pki.profile.normalize_label`, `P12Encryption`.
- Produces:
  - `CertificateSource(str, Enum)`: `ISSUED="issued"`, `UPLOADED="uploaded"`, `LEGACY="legacy"`, `CONFIGURED="configured"`;
    `CertificateState(str, Enum)`: `ACTIVE="active"`, `EXPIRED="expired"`, `REVOKED="revoked"`;
    `SessionRevokeCause.CERTIFICATE_REVOKED = "certificate_revoked"` (`CERTIFICATE_CHANGED` stays, unused).
  - `UserCertificate` (table `user_certificates`): columns of spec section 4 with types `id` int PK, `user_id` int FK
    `users.id` index, `source` str(16), `label` str(64) NOT NULL default `""`, `subject_cn` Optional str(255),
    `serial` Optional str(64), `not_before` / `not_after` Optional datetime, `fingerprint_hash` str(64) unique,
    `cn_hash` str(64), `cn_hash_scheme` SmallInteger, `serial_hash` str(64), `ca_issued` bool default False,
    `revoked_at` Optional datetime, `revoked_by` Optional int FK `users.id`, `revocation_reason` Optional str(32),
    `files_dir` Optional str(64), `export_count` int default 0, `last_exported_at` Optional datetime,
    `created_at` datetime, `created_by` Optional int FK `users.id`; index `ix_user_certificates_crl (ca_issued, revoked_at)`.
    `CertificateAuthorityState` (table `certificate_authority_state`): `id` int PK, `crl_number` int default 0,
    `this_update`, `next_update`, `published_at` Optional datetime, `revoked_count` int default 0.
  - `PkiConfig(BaseModel)` (`config.py`): fields and defaults of spec section 3 (`p12_encryption: P12Encryption`;
    `extra_crl_paths: list[str]` with a `mode="before"` validator using `_parse_json_sequence`);
    `def is_configured(self) -> bool` (the four paths set); `Settings.pki: PkiConfig = Field(default_factory=PkiConfig)`.
  - `auth/certificates.py`: `@dataclass(frozen=True) class CertificateValues: certificate: x509.Certificate; cn: str; serial: str; fingerprint: str; not_before: datetime; not_after: datetime`;
    `def certificate_values(cert_data: bytes) -> CertificateValues` (wraps `CertificateParser.parse_certificate`; same
    `ValueError`); `cn_hash_matches(certificate_cn)` now builds its `or_` over `UserCertificate.cn_hash` /
    `UserCertificate.cn_hash_scheme`.
  - `services/certificates.py`: `def certificate_state(row: UserCertificate, now: Optional[datetime] = None) -> CertificateState`;
    `def state_clause(state: CertificateState, now: Optional[datetime] = None)` (SQL condition);
    `def ca_issued(cert: x509.Certificate) -> bool` (False without `settings.pki.ca_cert_path`; the CA certificate
    cached per path and mtime);
    `async def register_certificate(session: AsyncSession, user: User, cert_data: bytes, label: Optional[str], source: CertificateSource, created_by: Optional[int]) -> UserCertificate`
    (`CodedError(ErrorCode.CERTIFICATE_INVALID)` on a parse failure; `CodedError(ErrorCode.CERTIFICATE_TAKEN)` when the
    fingerprint hash exists; label = `label` or the CN cut to 64; plain values and the three hashes; `ca_issued`;
    adds and flushes, no commit);
    `async def active_certificate(session: AsyncSession, user_id: int, fingerprint_hash: str) -> Optional[UserCertificate]`.
  - `AuthErrorCode.CERTIFICATE_REVOKED = ("certificate_revoked", problem(401, "Certificate revoked"))`, declared by
    login; `CERTIFICATE_TAKEN` title `"Certificate already registered"`;
    `FieldErrorCode.CERTIFICATE_LABEL_INVALID = ("certificate_label_invalid", field("Certificate label must be 1 to 64 characters without control characters"))`.
  - `AuthService.authenticate_pki` (same signature) matches `UserCertificate` joined to `User`: revoked ->
    `CERTIFICATE_REVOKED`, expired -> `CERTIFICATE_NOT_RECOGNIZED`, then the existing user checks.
  - `AuthService.end_session_of_replaced_certificate` renamed `end_session_of_revoked_certificate(session, user_session, user) -> bool`:
    a bound session whose fingerprint hash is not an active row of the user ends with `CERTIFICATE_REVOKED`; both
    callers (`dependencies.py:81`, `api/v1/auth.py:199`) updated.
  - `POST /users/`: `UserCreateForm.certificate: Optional[UploadFile] = None`, `certificate_label: Optional[str]`
    (validated with `normalize_label`, `invalid(FieldErrorCode.CERTIFICATE_LABEL_INVALID)`); with a file the route
    calls `register_certificate(..., CertificateSource.UPLOADED, auth.user.id)` after the user flush.
    `PATCH /users/{id}`: `UserUpdateForm` loses `certificate`; the certificate block and the
    `CERTIFICATE_CHANGED` cause leave `update_user`; its `problems(...)` lose the certificate codes.
    `taken_error_code` maps a unique violation naming `fingerprint_hash` to `CERTIFICATE_TAKEN`.
  - Seed (development): the admin's hashes go into a `configured` row. `init_user`: `cert_path` optional
    (`create_user(username, username_alias, cert_path: Optional[str], user_type, label: Optional[str] = None)`,
    `--label`); with a file an `uploaded` row through `register_certificate`.
  - `tests/helpers.py`: `async def make_certificate_row(db, user: User, source: CertificateSource = CertificateSource.UPLOADED, cert: Optional[x509.Certificate] = None, **values) -> UserCertificate`.

- [ ] **Step 1: Write the failing tests** in `tests/test_certificate_rows.py` and `tests/test_migration_024_certificates.py`:

```python
async def test_create_user_with_certificate_writes_an_uploaded_row(client, db, headers): ...
    # POST /users/ with the file and certificate_label "Office PC" -> 200; one row: source uploaded, label "Office PC",
    # subject_cn, serial (lower hex), not_after set, ca_issued False
async def test_create_user_without_certificate(client, db, headers): ...   # 200, no row
async def test_label_defaults_to_the_cn_and_is_validated(client, db, headers): ...  # no label -> CN; label "a"*65 -> 422 certificate_label_invalid
async def test_upload_signed_by_the_configured_ca_is_ca_issued(client, db, headers, tmp_path, monkeypatch): ...
    # settings.pki.ca_cert_path = written client_ca(); leaf of client_ca -> ca_issued True; leaf of legacy_root -> False
async def test_same_certificate_twice_is_taken(client, db, headers): ...   # second user with the same file -> 400 certificate_taken
async def test_patch_user_no_longer_takes_a_certificate(client, db, headers): ...  # "certificate" form field ignored, rows unchanged
async def test_revoked_certificate_login_is_401_certificate_revoked(client, db, headers, production): ...
async def test_expired_certificate_login_is_not_recognized(client, db, headers, production): ...
async def test_session_of_a_revoked_certificate_ends(client, db, headers, production): ...
    # log in, set revoked_at on the row directly -> next request 401 token_certificate_mismatch, session revoke_cause certificate_revoked
async def test_legacy_and_configured_rows_log_in(client, db, headers, production): ...

@pytest.mark.slow
def test_upgrade_turns_user_hashes_into_legacy_rows(tmp_path): ...
    # 023 database with two users (scheme 1 and 2) -> migrate: one legacy row each with the exact hashes, scheme and
    # created_at; users has no pki_certificate_* column; state row id 1, crl_number 0
@pytest.mark.slow
def test_downgrade_restores_exactly(tmp_path): ...      # snapshot of users before == after downgrade to 023; tables gone
@pytest.mark.slow
def test_downgrade_takes_the_newest_active_row(tmp_path): ...  # user with only uploaded rows (one revoked) -> hashes of the newest active one
@pytest.mark.slow
def test_downgrade_placeholder_for_a_taken_cn_hash(tmp_path): ...   # Review Focus 2
    # two users without legacy rows whose newest rows share cn_hash -> lower id keeps it, the other gets
    # "unassigned:<user_id>" in all three columns, the downgrade output names that user
```

- [ ] **Step 2: Run them to verify they fail**

Run: `venv\Scripts\python -m pytest tests/test_certificate_rows.py tests/test_migration_024_certificates.py -q`
Expected: FAIL (`ImportError` on `UserCertificate`)

- [ ] **Step 3: Implement** the enums, model, `PkiConfig`, migration 024 (upgrade: guarded `create_table` x2, insert
  state row, `INSERT INTO user_certificates ... SELECT ... FROM users` with source `legacy`, label `""`,
  `ca_issued` false, `export_count` 0, `created_at` = `users.created_at`; drop the unique indexes of the four
  columns, then the columns, in `batch_alter_table`; downgrade: add the columns nullable, fill per spec section 4
  printing every placeholder user and the count of dropped issued rows, set NOT NULL, recreate the unique indexes,
  drop both tables), the service functions, login, session check, user routes, seed and `init_user`.
  Adapt the existing tests that read `users.pki_certificate_*` (`test_pki_login.py`, `test_cn_hash_scheme.py`: the 019
  test now migrates to 024, so it checks the scheme on the `legacy` rows).

- [ ] **Step 4: Verify no reader of the old columns is left**

Run: `git grep -n "pki_certificate_" -- app tests`
Expected: only migrations, `AdminConfig` / `.env` setting names, `seed.py` and `init_user.py` reading those settings,
and `test_migration_024_certificates.py`.

- [ ] **Step 5: Run the tests and the whole suite**

Run: `venv\Scripts\python -m pytest tests/test_certificate_rows.py tests/test_migration_024_certificates.py tests/test_pki_login.py tests/test_cn_hash_scheme.py tests/test_migrations.py -q` then `venv\Scripts\python -m pytest -q`
Expected: PASS (after regenerating the contract and adding the CHANGELOG lines: `user_certificates`, `POST /users/`
certificate optional + `certificate_label`, `PATCH /users/{id}` without `certificate`, `certificate_revoked`, session
cause `certificate_revoked`)

- [ ] **Step 6: Commit**

```bash
git add app/backend/models app/backend/migrations/versions/024_user_certificates.py app/backend/migrations/versions/002_sync_schema_with_models.py app/backend/services/certificates.py app/backend/enums app/backend/config.py app/backend/auth app/backend/api/v1/auth.py app/backend/api/v1/users.py app/backend/api/v1/models/request/users.py app/backend/bootstrap/seed.py app/backend/cli/init_user.py tests CHANGELOG.md api-contract/v1/openapi.json
git commit -m "Certificates move to user_certificates: legacy rows, login and sessions per certificate (migration 024)"
```

(`git add tests` stages only tracked and new test files; check `git status` first that nothing outside the task is staged.)

---

### Task 5: PKI configuration, signer client, CRL publishing, health

**Files:**
- Create: `app/backend/services/signer_client.py`, `app/backend/services/crl_publisher.py`, `app/backend/bootstrap/pki.py`,
  `app/backend/cli/pki.py`, `app/backend/api/v1/certificates.py`, `app/backend/api/v1/models/response/certificates.py`
- Modify: `app/main.py` (lifespan), `app/backend/api/system/health_checks.py`, `app/backend/api/system/health.py`,
  `app/backend/enums/routes.py` (`CertificateRoutes` with `BASE`, `AUTHORITY`; in `class Routes`),
  `app/backend/enums/errors.py` (`CERTIFICATE_AUTHORITY_NOT_CONFIGURED`), `app/backend/api/v1/__init__.py`,
  `tests/conftest.py` (fixture `pki`), `tests/pki_helpers.py` (`DownTransport`), `CHANGELOG.md`, `api-contract/v1/openapi.json`
- Test: `tests/test_crl_publisher.py`, `tests/test_pki_settings.py`, `tests/test_pki_health.py`

**Interfaces:**
- Consumes: Task 1 `pki.crl`, `pki.store.write_atomically`, `pki.authority.load_ca_certificate`; Task 2 `create_app`,
  `SignerSettings` (tests only); Task 4 `UserCertificate`, `CertificateAuthorityState`, `PkiConfig`.
- Produces:
  - `services/signer_client.py`: `SIGNER_TIMEOUT_SECONDS = 10`; `class SignerUnavailable(Exception)`;
    `class SignerRefused(RuntimeError)` (status and reason of a 4xx/5xx answer; reaches the 500 handler);
    `@dataclass(frozen=True) class SignedCertificate: certificate_pem: str; serial: str`;
    `@dataclass(frozen=True) class SignerStatus: subject: str; not_after: datetime; fingerprint_sha256: str`;
    `class SignerClient` with `__init__(self, transport: httpx.AsyncBaseTransport)`,
    `async def sign_client(self, public_key_pem: str, label: str, validity_days: int) -> SignedCertificate`,
    `async def sign_crl(self, crl_number: int, this_update: datetime, next_update: datetime, entries: Sequence[CrlEntry]) -> str`,
    `async def health(self) -> SignerStatus` (`httpx.TransportError` -> `SignerUnavailable`);
    `def signer_client() -> SignerClient` (`httpx.AsyncHTTPTransport(uds=settings.pki.signer_socket)`, base URL
    `http://signer`) and `def use_signer_transport(transport: Optional[httpx.AsyncBaseTransport]) -> None` (tests).
  - `services/crl_publisher.py`: `@dataclass(frozen=True) class CrlStatus: this_update: Optional[datetime]; next_update: Optional[datetime]; revoked_count: int; published_at: Optional[datetime]; stale: bool; earliest_next_update: Optional[datetime]`;
    `async def publish_crl(session: AsyncSession, only_if_due: bool = False) -> bool` (spec section 7 steps 1-5;
    `with_for_update()` on the state row; `only_if_due` re-checks inside the lock: file missing or now past
    `this_update + (next_update - this_update) / 2`; on `SignerUnavailable` rolls back, logs and returns False; True
    when the file was written; no-op returning False when PKI is not configured);
    `async def crl_status(session: AsyncSession) -> CrlStatus` (`stale`: a `ca_issued` row with `revoked_at` after
    `published_at`, or revoked rows and never published; `earliest_next_update` from the file via `earliest_next_update`).
  - `bootstrap/pki.py`: `class PkiSettingsError(RuntimeError)`; `def check_pki_settings(pki: PkiConfig) -> None`
    (partial configuration names the missing settings; configured: `load_ca_certificate(ca_cert_path)`, `store_dir`
    and the directory of `crl_path` writable, every `extra_crl_paths` file parses with `split_pem_crls`);
    `async def publish_crl_at_startup() -> None` (configured only; `publish_crl(only_if_due=True)`).
    `app/main.py` lifespan: `check_pki_settings(settings.pki)` right after `check_deployment(settings)`,
    `await publish_crl_at_startup()` after `initialize_database_with_migrations()`.
  - `CertificateRoutes(str, Enum)`: `BASE = "/certificates"`, `AUTHORITY = "/authority"`, `tag() -> "CERTIFICATES"`
    (`ROOT` comes in Task 6).
  - `ErrorCode.CERTIFICATE_AUTHORITY_NOT_CONFIGURED = ("certificate_authority_not_configured", problem(503, "Certificate authority not configured"))`.
  - `GET /certificates/authority` (`require()`): `CertificateAuthorityResponse(subject: str, not_after: ResponseDatetime, fingerprint_sha256: str, certificate_pem: str, crl: CrlInfo)`,
    `CrlInfo(this_update: Optional[ResponseDatetime], next_update: Optional[ResponseDatetime], revoked_count: int)`;
    503 `certificate_authority_not_configured` without PKI. Module `api/v1/certificates.py`, added to `V1` after `users`.
  - Health: `PkiHealthModel(status: str, ca_expires_in_days: int, crl_next_update_in_days: Optional[int], crl_stale: bool, signer_available: bool)`;
    `HealthResponse.pki: Optional[PkiHealthModel] = None` (only when configured);
    `async def check_pki_health(session) -> Optional[PkiHealthModel]` in `health_checks.py`; `degraded` when a day
    count is below 2 or missing, `crl_stale`, or not `signer_available`; overall status `degraded` then.
  - `cli/pki.py`: `async def run(argv: list[str]) -> int` (sessions from `get_session_maker()`) and
    `def main(argv: Optional[list[str]] = None) -> int` (`asyncio.run(run(argv or sys.argv[1:]))`), subcommands `crl`
    (publishes now, exit 1 when the signer is unavailable) and `status` (prints spec section 8's `status` lines).
  - `tests/conftest.py`: fixture `pki(tmp_path, monkeypatch) -> PkiEnv` with `@dataclass PkiEnv: ca: tuple; store_dir: Path; crl_path: Path; signer_settings: SignerSettings`
    and `def signer_down(self) -> None` / `def signer_up(self) -> None`; sets every `settings.pki` path, installs
    `use_signer_transport(httpx.ASGITransport(app=create_app(...)))`, resets it at teardown;
    `tests/pki_helpers.py`: `class DownTransport(httpx.AsyncBaseTransport)` raising `httpx.ConnectError`.

- [ ] **Step 1: Write the failing tests**:

```python
# tests/test_crl_publisher.py
async def test_publish_writes_a_signed_crl_with_the_extra_crls(db, pki): ...
    # one revoked ca_issued row, one revoked foreign upload, one expired revoked ca_issued row, an extra CRL file of
    # legacy_root -> publish_crl True; split_pem_crls(file) == 2; first lists only the first serial, number 1, signature
    # valid against pki.ca; second equals the extra file; state row number 1, revoked_count 1
async def test_number_grows(db, pki): ...                        # two publishes -> CRLNumber 1 then 2
async def test_only_if_due_skips_a_fresh_crl(db, pki): ...      # publish, then only_if_due -> False, file unchanged
async def test_signer_down_keeps_the_number(db, pki): ...       # signer_down -> False, state row unchanged, no file
async def test_status_is_stale_after_an_unpublished_revocation(db, pki): ...

# tests/test_pki_settings.py
def test_partial_configuration_refuses_to_start(): ...          # only store_dir set -> PkiSettingsError naming the 3 others
def test_bad_ca_certificate_refuses_to_start(tmp_path): ...     # a leaf as ca_cert_path -> PkiSettingsError
def test_unparsable_extra_crl_refuses_to_start(tmp_path): ...
def test_not_configured_is_fine(): ...
async def test_authority_route(client, db, headers, pki): ...   # 200 subject, fingerprint, crl fields; without pki -> 503 certificate_authority_not_configured

# tests/test_pki_health.py
async def test_health_has_no_pki_part_without_configuration(client, db): ...
async def test_health_reports_pki(client, db, pki): ...          # after publish: healthy, crl_next_update_in_days 7 (or 6), signer_available True
async def test_health_degraded_when_the_signer_is_down(client, db, pki): ...
async def test_health_reports_the_earliest_next_update_of_an_extra_crl(client, db, pki): ...   # Review Focus 3
    # extra CRL of legacy_root with next_update in 1 day -> crl_next_update_in_days 1, status degraded
```

- [ ] **Step 2: Run them to verify they fail**

Run: `venv\Scripts\python -m pytest tests/test_crl_publisher.py tests/test_pki_settings.py tests/test_pki_health.py -q`
Expected: FAIL (`ModuleNotFoundError: app.backend.services.crl_publisher`)

- [ ] **Step 3: Implement** the client, publisher, bootstrap, route, health part, CLI and fixture.

- [ ] **Step 4: Run the tests and the whole suite**

Run: `venv\Scripts\python -m pytest tests/test_crl_publisher.py tests/test_pki_settings.py tests/test_pki_health.py tests/test_route_registry.py tests/test_openapi.py -q` then `venv\Scripts\python -m pytest -q`
Expected: PASS (after regenerating the contract and the CHANGELOG lines: `GET /certificates/authority`, `/health` `pki`)

- [ ] **Step 5: Commit**

```bash
git add app/backend/services/signer_client.py app/backend/services/crl_publisher.py app/backend/bootstrap/pki.py app/backend/cli/pki.py app/backend/api/v1/certificates.py app/backend/api/v1/models/response/certificates.py app/backend/api/v1/__init__.py app/backend/api/system app/main.py app/backend/enums tests/conftest.py tests/pki_helpers.py tests/test_crl_publisher.py tests/test_pki_settings.py tests/test_pki_health.py CHANGELOG.md api-contract/v1/openapi.json
git commit -m "PKI settings, signer client, CRL publishing at startup and daily, health and authority route"
```

---

### Task 6: Certificate routes: permissions, issue, upload, list, export

**Files:**
- Create: `app/backend/api/v1/user_certificates.py`, `app/backend/api/v1/models/request/certificates.py`,
  `app/backend/api/v1/models/query/certificates.py`, `app/backend/filters/certificate_filter.py`, `tests/test_certificate_routes.py`
- Modify: `app/backend/enums/general.py` (`Permissions.CERTIFICATE_VIEW/CREATE/REVOKE/EXPORT` = `certificate.view` ...),
  `app/backend/bootstrap/permissions.py` (`PERMISSION_META` Ukrainian names; `_SYSTEM_PERMISSIONS` += `certificate.export`),
  `app/backend/auth/user_types.py` (`POST_ADMIN_BASE` += view, create, revoke), `app/backend/enums/routes.py`
  (`UserRoutes`: `USER_CERTIFICATES = "/{user_id}/certificates"`, `USER_CERTIFICATE_UPLOAD = "/{user_id}/certificates/upload"`,
  `USER_CERTIFICATE_BY_ID = "/{user_id}/certificates/{certificate_id}"`,
  `USER_CERTIFICATE_EXPORT = "/{user_id}/certificates/{certificate_id}/export"`; `CertificateRoutes.ROOT = "/"`;
  the revoke member comes with its route in Task 7, since a member without a route fails the registry test),
  `app/backend/auth/access_control.py` (`check_user_permission`),
  `app/backend/enums/errors.py`, `app/backend/services/certificates.py`, `app/backend/api/v1/certificates.py`,
  `app/backend/api/v1/models/response/certificates.py`, `app/backend/api/v1/auth.py` + `models/response/auth.py`
  (`/auth/me` `certificate`), `app/backend/api/v1/__init__.py` (`user_certificates` after `users`),
  `app/backend/cli/pki.py` (`issue`), `CHANGELOG.md`, `api-contract/v1/openapi.json`
- Test: `tests/test_certificate_routes.py`

**Interfaces:**
- Consumes: Tasks 1, 4, 5 (`PkiEnv` fixture, `signer_client()`, `register_certificate`, `certificate_state`, `state_clause`).
- Produces:
  - Error codes: `CERTIFICATE_NOT_FOUND` (404, "Certificate not found"), `CERTIFICATE_AUTHORITY_UNAVAILABLE` (503,
    "Certificate authority unavailable"), `CERTIFICATE_NOT_ACTIVE` (409, "Certificate is not active"),
    `CERTIFICATE_NOT_EXPORTABLE` (409, "Certificate has no stored package"), `CERTIFICATE_FILES_MISSING` (410,
    "Certificate files missing"); `FieldErrorCode.VALIDITY_DAYS_TOO_LONG = ("validity_days_too_long", field("Validity is at most {max_days} days", max_days=int))`
    (the limit is a setting, so a code of its own instead of pydantic's static `le`).
  - `services/certificates.py`: `@dataclass(frozen=True) class CertificatePackage: certificate: UserCertificate; p12: bytes; password: str; filename: str`;
    `async def get_user_certificate(session, user_id: int, certificate_id: int, lock: bool = False) -> UserCertificate`
    (`CERTIFICATE_NOT_FOUND`, also for another user's id);
    `async def issue_certificate(session, user: User, label: str, validity_days: int, created_by: Optional[int], write_audit: Callable[[UserCertificate], Awaitable[None]]) -> CertificatePackage`
    (spec section 5 steps 1-6: user row `with_for_update()`, archived -> `CodedError(USER_NOT_FOUND)`, not configured ->
    `CERTIFICATE_AUTHORITY_NOT_CONFIGURED`, `SignerUnavailable` -> `CERTIFICATE_AUTHORITY_UNAVAILABLE`, row with the
    hashes of `certificate_values(pem)`, `files_dir`, `ca_issued` True, `write_certificate_dir`, `write_audit(row)`,
    commit; any failure after the folder: rollback, `remove_certificate_dir`, re-raise);
    `async def export_package(session, certificate: UserCertificate, write_audit: Callable[[UserCertificate], Awaitable[None]]) -> CertificatePackage`
    (not configured, not `issued` -> `CERTIFICATE_NOT_EXPORTABLE`, not active -> `CERTIFICATE_NOT_ACTIVE`, folder gone
    -> `CERTIFICATE_FILES_MISSING`; `export_count + 1`, `last_exported_at`, audit, commit).
  - `filters/certificate_filter.py`: `@dataclass(frozen=True, kw_only=True) class CertificateCriteria(ListCriteria)`:
    `state: Optional[CertificateState] = None`, `source: Optional[CertificateSource] = None`, `user_id: Optional[int] = None`,
    `post_id: Optional[int] = None`, `expires_within_days: Optional[int] = None`;
    `class CertificateFilterService(SimpleFilterEngine)`: `MODEL = UserCertificate`, `CRITERIA = CertificateCriteria`,
    `SORTABLE_FIELDS = ["id", "label", "not_after", "created_at"]`,
    `apply_filters(cls, query, params, home_post_ids: Optional[Iterable[int]] = None)` (joins `User`, non-archived
    users, `User.post_id` in the scope; `None` = every user; users without a home post only when the scope is `None`).
  - `api/v1/models/query/certificates.py`: `CertificateSortField = Literal["id", "label", "not_after", "created_at"]`;
    `CertificateFilterParams(BaseFilterParams)` with the same field names (`expires_within_days: Optional[int] = Field(default=None, ge=0, le=3650)`).
  - Request: `CertificateIssueRequest(label: str, validity_days: Optional[int] = Field(default=None, ge=1))`
    (`label` through `normalize_label`, else `invalid(FieldErrorCode.CERTIFICATE_LABEL_INVALID)`; `validity_days` above
    `settings.pki.max_validity_days` -> `invalid(FieldErrorCode.VALIDITY_DAYS_TOO_LONG, max_days=...)`);
    upload form `CertificateUploadForm(file: UploadFile, label: str)` (same label rule).
  - Response: `CertificateResponse` (spec section 6: every column but hashes and `files_dir`, plus `state:
    CertificateState`, `has_package: bool`); `def certificate_to_response(row: UserCertificate, now: Optional[datetime] = None) -> CertificateResponse`;
    `CertificatePackageResponse(certificate: CertificateResponse, p12_base64: str, password: str, filename: str)`;
    `UserInfoResponse.certificate: Optional[SessionCertificate]` with `SessionCertificate(id: int, label: str, not_after: Optional[ResponseDatetime], state: CertificateState)`.
  - `AccessControlService.check_user_permission(auth: AuthContext, permission: Union[Permissions, str], target: User) -> None`
    (`auth.covers_user(permission, target)`, else `access_denied(AccessErrorCode.PERMISSION_DENIED, missing=[name])`).
  - Routes (spec section 6 table; checks: `_get_user_or_404` style lookup, `check_user_view`, archived user ->
    `USER_NOT_FOUND` unless `user.archive` covers the user as in `get_user`, then
    `check_user_permission(auth, Permissions.CERTIFICATE_VIEW, user)`; writes add
    `check_target_user(session, auth, user, Permissions.CERTIFICATE_X)`): `list_user_certificates`,
    `issue_user_certificate` (201, `Cache-Control: no-store`), `upload_user_certificate` (201), `get_user_certificate_by_id`,
    `export_user_certificate` (200, `Cache-Control: no-store`), `list_certificates` (`GET /certificates/`, scope
    `AccessControlService.scope_for_all(auth, CERTIFICATE_VIEW, USER_VIEW)` narrowed by `filters.post_id`).
    Audit rows `resource_type="user_certificate"`, actions `issue`, `upload`, `export`; details hold label, serial,
    `sha256_fingerprint`, `not_after`; never key, `.p12` or password.
  - `cli/pki.py issue --username U --label L [--days N] --out DIR` (spec section 8; `created_by` None, audit details
    `{"via": "cli", ...}`; writes `<label-file>.p12` and `password.txt` into `DIR`).

- [ ] **Step 1: Write the failing tests** in `tests/test_certificate_routes.py`:

```python
async def test_issue_returns_the_package_once(client, db, headers, pki):
    admin = await make_admin(db); user = await make_user(db)
    r = await client.post(V1.path(UserRoutes.USER_CERTIFICATES, user_id=user.id), json={"label": "Laptop"}, headers=headers(admin))
    assert r.status_code == 201 and r.headers["cache-control"] == "no-store"
    body = r.json()
    loaded = pkcs12.load_pkcs12(base64.b64decode(body["p12_base64"]), body["password"].encode())
    assert loaded.cert.friendly_name == b"Laptop" and is_signed_by(loaded.cert.certificate, pki.ca[0])
    assert body["filename"] == "Laptop.p12" and body["certificate"]["state"] == "active" and body["certificate"]["has_package"]
    assert (pki.store_dir / f"{user.id}" / f"{body['certificate']['id']}" / "certificate.p12").exists()

async def test_issued_certificate_logs_in_through_nginx(client, db, headers, pki, production): ...
async def test_issued_certificate_logs_in_through_the_development_json(client, db, headers, pki): ...  # ASCII label: the JSON login's CN pattern is ASCII
async def test_cyrillic_label_logs_in_through_nginx(client, db, headers, pki, production): ...   # Review Focus 1, openssl_rfc2253 DN
async def test_failed_issue_leaves_no_directory(client, db, headers, pki, monkeypatch): ...      # Review Focus 4: create_audit_log raises -> 500, no row, store_dir/<user_id> empty
async def test_issue_without_pki_or_signer(client, db, headers, pki): ...   # unconfigured -> 503 certificate_authority_not_configured; signer_down -> 503 certificate_authority_unavailable, no row
async def test_issue_to_an_archived_user_is_404(client, db, headers, pki): ...
async def test_label_and_validity_errors(client, db, headers, pki): ...      # "" / 65 chars -> certificate_label_invalid; 731 -> validity_days_too_long max_days 730
async def test_upload_route(client, db, headers, pki): ...                   # leaf of pki.ca -> 201 ca_issued True; legacy_root leaf -> False; again -> 400 certificate_taken; an expired leaf -> 201 state expired
async def test_export_counts_and_audits(client, db, headers, pki): ...       # same p12 bytes and password as issued, export_count 1, audit action export
async def test_export_refusals(client, db, headers, pki): ...                # uploaded -> 409 certificate_not_exportable; revoked_at set -> 409 certificate_not_active; folder removed -> 410 certificate_files_missing
async def test_post_admin_cannot_export(client, db, headers, pki): ...       # post_admin of the user's post -> 403 (certificate.export is system)
async def test_delegation_inside_and_outside_scope(client, db, headers, pki): ...
    # post_admin at group East: issue for a user of East -> 201; for a user of West -> 403; for a user who holds a
    # permission the post_admin lacks -> 403 target_user_more_privileged
async def test_check_order(client, db, headers, pki): ...                    # outsider asking for a missing certificate of a foreign user -> 403, not 404
async def test_another_users_certificate_is_404(client, db, headers, pki): ...
async def test_list_and_filters(client, db, headers, pki): ...               # per-user list state filter; /certificates/ state, source, user_id, post_id, expires_within_days; archived users left out
async def test_me_shows_the_session_certificate(client, db, headers, pki, production): ...  # login with an issued cert -> /auth/me certificate.label; development login -> null
@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
async def test_issued_files_permissions(client, db, headers, pki): ...
async def test_cli_issue(session_maker, db, pki, tmp_path, monkeypatch): ...
    # monkeypatch app.backend.cli.pki.get_session_maker to return the test session_maker;
    # await run(["issue", "--username", user.username, "--label", "Laptop", "--out", str(tmp_path / "out")]) == 0;
    # one row with created_by None; out has Laptop.p12 and password.txt
```

- [ ] **Step 2: Run them to verify they fail**

Run: `venv\Scripts\python -m pytest tests/test_certificate_routes.py -q`
Expected: FAIL (`AttributeError: USER_CERTIFICATES`)

- [ ] **Step 3: Implement** permissions, user type base, enums, codes, services, filter, models, routes, `/auth/me`
  and the CLI command.

- [ ] **Step 4: Run the tests and the whole suite**

Run: `venv\Scripts\python -m pytest tests/test_certificate_routes.py tests/test_route_access.py tests/test_route_registry.py tests/test_openapi.py -q` then `venv\Scripts\python -m pytest -q`
Expected: PASS (after regenerating the contract and the CHANGELOG lines)

- [ ] **Step 5: Commit**

```bash
git add app/backend/api/v1 app/backend/filters/certificate_filter.py app/backend/services/certificates.py app/backend/enums app/backend/bootstrap/permissions.py app/backend/auth/user_types.py app/backend/cli/pki.py tests/test_certificate_routes.py CHANGELOG.md api-contract/v1/openapi.json
git commit -m "Certificate routes: issue, upload, list, export, authority; certificate permissions; /auth/me certificate"
```

---

### Task 7: Revocation

**Files:**
- Modify: `app/backend/services/certificates.py`, `app/backend/auth/service.py`, `app/backend/api/v1/user_certificates.py`,
  `app/backend/api/v1/models/request/certificates.py`, `app/backend/enums/errors.py`, `app/backend/enums/routes.py`
  (`UserRoutes.USER_CERTIFICATE_REVOKE = "/{user_id}/certificates/{certificate_id}/revoke"`), `CHANGELOG.md`,
  `api-contract/v1/openapi.json`
- Test: `tests/test_certificate_revocation.py`

**Interfaces:**
- Consumes: Task 4 `SessionRevokeCause.CERTIFICATE_REVOKED`, `end_session_of_revoked_certificate`; Task 5 `publish_crl`,
  `crl_status`; Task 6 routes, `get_user_certificate`.
- Produces:
  - `ErrorCode.CERTIFICATE_ALREADY_REVOKED = ("certificate_already_revoked", problem(409, "Certificate already revoked"))`.
  - `AuthService.invalidate_certificate_sessions(session: AsyncSession, user_id: int, fingerprint_hash: str, cause: SessionRevokeCause, actor_user_id: Optional[int] = None, ip_address: Optional[str] = None, user_agent: Optional[str] = None, commit: bool = True) -> int`
    (same behaviour as `invalidate_all_user_sessions`, only live sessions with that `certificate_fingerprint_hash`).
  - `async def revoke_certificate(session, certificate: UserCertificate, reason: RevocationReason, revoked_by: int, write_audit: Callable[[UserCertificate], Awaitable[None]]) -> UserCertificate`
    (row locked; `CERTIFICATE_ALREADY_REVOKED`; sets `revoked_at`, `revoked_by`, `revocation_reason`; ends its sessions
    with `commit=False`; audit; commit).
  - `CertificateRevokeRequest(reason: RevocationReason = RevocationReason.UNSPECIFIED)`.
  - Route `revoke_user_certificate` (`POST UserRoutes.USER_CERTIFICATE_REVOKE`, `require(all=[CERTIFICATE_VIEW, CERTIFICATE_REVOKE])`,
    checks as Task 6 writes, then the certificate, then its state): 200 `CertificateResponse`; after the commit, when
    the row is `ca_issued` and PKI is configured, `await publish_crl(session)` (a False result is logged, the answer
    stays 200). Audit action `revoke` with the reason.

- [ ] **Step 1: Write the failing tests** in `tests/test_certificate_revocation.py`:

```python
async def test_revoke_lists_the_serial_on_the_crl(client, db, headers, pki): ...
    # issue, revoke {"reason": "key_compromise"} -> 200 state revoked; CRL file lists the serial with keyCompromise; audit revoke
async def test_revoke_twice_is_409(client, db, headers, pki): ...            # certificate_already_revoked
async def test_revoke_ends_only_that_devices_sessions(client, db, headers, pki, production): ...
    # two issued certificates of one user, a login with each; revoke the first -> its session 401 token_certificate_mismatch
    # (revoke_cause certificate_revoked), the second keeps working; login with the first -> 401 certificate_revoked
async def test_revoking_a_foreign_upload_blocks_login_but_not_the_crl(client, db, headers, pki, production): ...
    # uploaded legacy_root leaf: revoke -> login 401 certificate_revoked, CRL file has no entry for it
async def test_revoking_a_legacy_row_works_without_pki(client, db, headers): ...
async def test_revocation_while_the_signer_is_down_reaches_the_next_crl(client, db, headers, pki): ...   # Review Focus 5
    # signer_down, revoke -> 200; crl_status stale True; signer_up, publish_crl -> serial listed, stale False
async def test_revoke_permissions_and_scope(client, db, headers, pki): ...   # without certificate.revoke -> 403; post_admin outside scope -> 403
```

- [ ] **Step 2: Run them to verify they fail**

Run: `venv\Scripts\python -m pytest tests/test_certificate_revocation.py -q`
Expected: FAIL (`AttributeError: USER_CERTIFICATE_REVOKE`)

- [ ] **Step 3: Implement** the service, the session helper and the route.

- [ ] **Step 4: Run the tests and the whole suite**

Run: `venv\Scripts\python -m pytest tests/test_certificate_revocation.py -q` then `venv\Scripts\python -m pytest -q`
Expected: PASS (after regenerating the contract and the CHANGELOG lines)

- [ ] **Step 5: Commit**

```bash
git add app/backend/services/certificates.py app/backend/auth/service.py app/backend/api/v1/user_certificates.py app/backend/api/v1/models/request/certificates.py app/backend/enums/errors.py tests/test_certificate_revocation.py CHANGELOG.md api-contract/v1/openapi.json
git commit -m "Certificate revocation: ends that device's sessions, publishes the CRL after the commit"
```

---

### Task 8: Deployment files

**Files:**
- Create: `signer.env.example`, `deploy/systemd/watch-web-crl.path`, `deploy/systemd/watch-web-crl.service`,
  `deploy/systemd/watch-web-crl-refresh.timer`, `deploy/systemd/watch-web-crl-refresh.service`,
  `deploy/pki/remove-old-root.cmd`, `tests/test_pki_deployment.py`
- Modify: `docker-compose.yml`, `nginx.conf`, `.env.example`, `tests/test_nginx_conf.py`
- Test: `tests/test_pki_deployment.py`, `tests/test_nginx_conf.py`

**Interfaces:**
- Produces:
  - `docker-compose.yml`: service `pki-signer` (not the `x-app` anchor): `image: watch-web`, `network_mode: none`,
    `env_file: signer.env`, `user: "${PKI_SIGNER_UID:?set PKI_SIGNER_UID in .env}:${MEDIA_GID:-10001}"`,
    `command: ["python", "-m", "app.backend.signer", "serve"]`, volumes `${PKI_DIR}/ca:${PKI_DIR}/ca:ro`,
    `${PKI_DIR}/private:${PKI_DIR}/private:ro`, `${PKI_DIR}/run:${PKI_DIR}/run`, `restart: unless-stopped`, the same
    logging block; `api` gains `MYSETTINGS_PKI__CA_CERT_PATH: ${PKI_DIR}/ca/client-ca.crt`,
    `MYSETTINGS_PKI__SIGNER_SOCKET: ${PKI_DIR}/run/signer.sock`, `MYSETTINGS_PKI__STORE_DIR: ${PKI_DIR}/issued`,
    `MYSETTINGS_PKI__CRL_PATH: ${PKI_DIR}/crl/nginx.crl` and volumes `ca:ro`, `crl`, `issued`, `run` (same absolute
    paths), `depends_on: [pki-signer]`. PKI stays optional: the four variables are commented in `.env.example` with a
    note that compose sets them from `PKI_DIR`.
  - `signer.env.example`: `SIGNER_CA_CERT_PATH`, `SIGNER_CA_KEY_PATH`, `SIGNER_CA_KEY_PASSWORD`, `SIGNER_SOCKET_PATH`,
    `SIGNER_MAX_VALIDITY_DAYS=730`, `SIGNER_MAX_CRL_DAYS=31` with the `PKI_DIR` paths of spec section 3.
  - `.env.example`: section `# ------------------------------------------------------------------ PKI` with
    `PKI_DIR`, `PKI_SIGNER_UID`, the optional `MYSETTINGS_PKI__*` (`EXTRA_CRL_PATHS` as a JSON list, `VALIDITY_DAYS`,
    `MAX_VALIDITY_DAYS`, `KEY_SIZE`, `P12_ENCRYPTION`, `CRL_DAYS`); the admin PKI block says it serves only the
    development seed.
  - `nginx.conf`: the server block of spec section 7 (Let's Encrypt paths, the bundle, `ssl_crl`, `ssl_verify_depth 1`)
    with comments naming `PKI.md`.
  - systemd: `watch-web-crl.path` (`[Path] PathChanged=/srv/watch_web/pki/crl/nginx.crl`, `Unit=watch-web-crl.service`,
    `[Install] WantedBy=multi-user.target`); `watch-web-crl.service` (`Type=oneshot`,
    `ExecStart=/bin/sh -c 'nginx -t && nginx -s reload'`); `watch-web-crl-refresh.timer` (`OnCalendar=daily`,
    `Persistent=true`, `RandomizedDelaySec=1h`); `watch-web-crl-refresh.service` (`Type=oneshot`,
    `WorkingDirectory=/opt/watch_web`, `ExecStart=/usr/bin/docker compose exec -T api python -m app.backend.cli.pki crl`).
    Paths marked `# edit:`.
  - `deploy/pki/remove-old-root.cmd`: `certutil -delstore Root <thumbprint>` (machine, needs administrator) or
    `certutil -user -delstore Root <thumbprint>` (`%1` = thumbprint, `%2` = `user` for the current user); echoes
    what it removes.

- [ ] **Step 1: Write the failing tests**:

```python
# tests/test_pki_deployment.py
def test_signer_has_no_network_and_its_own_env(): ...
    # services["pki-signer"]["network_mode"] == "none"; env_file == "signer.env"; command ends with "serve"
def test_only_the_signer_mounts_the_private_key(): ...
    # "/private" appears in pki-signer volumes only; api has ca (":ro"), crl, issued, run; media-worker and imgproxy have no PKI_DIR
def test_api_pki_settings_point_into_pki_dir(): ...
def test_systemd_units(): ...      # PathChanged= in the path unit, the service runs "nginx -t && nginx -s reload", the timer is daily and Persistent
def test_signer_env_example_names_every_signer_setting(): ...   # every SignerSettings field appears as SIGNER_<NAME>

# tests/test_nginx_conf.py, test_production_server_requires_a_verified_client_certificate gains:
    for directive in ("ssl_verify_depth 1;", "ssl_crl ", "/etc/letsencrypt/live/"):
        assert directive in tls[0], directive
```

- [ ] **Step 2: Run them to verify they fail**

Run: `venv\Scripts\python -m pytest tests/test_pki_deployment.py tests/test_nginx_conf.py -q`
Expected: FAIL (`KeyError: 'pki-signer'`)

- [ ] **Step 3: Write** the compose service, mounts, examples, nginx block, units and script.

- [ ] **Step 4: Run the tests and the whole suite**

Run: `venv\Scripts\python -m pytest tests/test_pki_deployment.py tests/test_nginx_conf.py tests/test_media_settings.py -q` then `venv\Scripts\python -m pytest -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add docker-compose.yml nginx.conf .env.example signer.env.example deploy tests/test_pki_deployment.py tests/test_nginx_conf.py
git commit -m "Deployment: pki-signer container, PKI_DIR mounts, nginx CRL and client CA bundle, systemd units"
```

---

### Task 9: Documents and live checks

**Files:**
- Create: `PKI.md`
- Modify: `CLAUDE.md` (a "PKI" section with the rules of spec sections 1-7 for future work, and the topology line:
  four containers, `pki-signer` without network), `API.md` (routes, codes, `/auth/me` certificate, the `.p12` flow),
  `DEPLOYMENT.md` (link to `PKI.md`, `PKI_DIR`, `PKI_SIGNER_UID`, order of steps of spec section 13), `CHANGELOG.md`
  (migration note for the web client: `PATCH /users/{id}` lost `certificate`, `POST /users/` certificate optional,
  new routes and codes; operators: migration 024), memory file
  `C:\Users\AdMi\.claude\projects\E--Develop-pypr-watch-web\memory\pki-certificates.md` (status: implemented)
- Test: the whole suite and the live runs below

- [ ] **Step 1: Write the documents** (ASCII, no emoji): `PKI.md` with the nine parts of spec section 9.

- [ ] **Step 2: Verify the contract is current**

Run: `venv\Scripts\python -m pytest tests/test_api_contract.py tests/test_openapi.py tests/test_error_contract.py tests/test_route_registry.py tests/test_route_access.py -q`
Expected: PASS

- [ ] **Step 3: Live migration check** on throwaway MySQL 8 and MariaDB 11 containers (as for 021-023; docker
  usage in memory `nginx-live-check.md` and `bash-heredoc-backslashes.md`): a 023 schema with two users (CN hash
  schemes 1 and 2), migrate to 024, compare the legacy rows with the old columns, downgrade to 023, compare the
  `users` table with the snapshot, upgrade again.
Expected: exact on both engines; no errors.

- [ ] **Step 4: Live nginx check** (spec section 11 items 2-3): `nginx:1.27` with a client CA from `signer init`, a
  `legacy_root` style root with a CRL from `signer sign-crl`, a server certificate, a `.p12` of each, the bundle and
  the CRL file published by the API through the real Unix socket (signer and api containers of the e2e stack, or
  `python -m app.backend.signer serve` and the API in a Linux container): `curl --cert-type P12` passes with both;
  revoke the new one through the API, reload nginx, the revoked one gets 400; set the old root's CRL `next_update` to
  the past (re-sign), reload: every certificate refused.
Expected: as described; record the commands and outputs in the task report.

- [ ] **Step 5: Live systemd check** in a systemd-capable container (for example `jrei/systemd-debian`) if it
  starts on this machine: install the path unit pointing at a test file, `os.replace` a new file into place, the
  service ran (`journalctl -u watch-web-crl.service`). If the rename does not trigger `PathChanged=`, switch the unit
  to watch the directory (spec section 7) and repeat. If no systemd container runs here, write the check as a step in
  `PKI.md` "Checks" for the staging server and say so in the report.

- [ ] **Step 6: Run the whole suite**

Run: `venv\Scripts\python -m pytest -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add PKI.md CLAUDE.md API.md DEPLOYMENT.md CHANGELOG.md
git commit -m "PKI documents: PKI.md guide, CLAUDE.md rules, API.md, DEPLOYMENT.md, CHANGELOG migration note"
```

(If Step 5 changed `deploy/systemd/watch-web-crl.path`, add it to this commit.)

---

## After the last task

A final fresh validator subagent (clean context: this plan, the spec, `git diff main...pki-certificates`) reviews
the whole branch for logic, security (who can issue for whom, export only at `/`, the key never leaving the signer,
nothing secret in audit rows or logs, the folder permissions), contract and migration correctness before the merge.
Then superpowers:finishing-a-development-branch. The user's own check remains: importing a `.p12` in their browser
and seeing the label in the picker (spec section 11 item 5), already covered by Task 3 for the throwaway CA.
