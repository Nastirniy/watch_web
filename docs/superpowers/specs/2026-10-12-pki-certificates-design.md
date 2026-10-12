# PKI: issuing and managing client certificates - design

Status: approved in conversation section by section (2026-10-12); this document awaits review.
Sub-project B of two (sub-project A: `2026-10-11-authorization-v2-design.md`, merged).

## Goal

The app issues the client certificates people log in with, binds them to users, and revokes them, so that:

- an administrator creates a user and hands over a working certificate without `openssl` or scripts;
- a person picks their certificate in the browser by a readable name;
- revoking a certificate locks that device out of the API at once and out of nginx within seconds;
- nginx always has a valid CRL;
- HTTPS works without browser warnings and without installing any certificate authority on client machines;
- no key the app holds online can be used to impersonate a website to the post machines.

Success:

- issuing, listing, revoking and re-downloading certificates are API features with the authorization v2 rules
  (scope, delegation, permission pairs, the check order);
- the client CA key is readable only by a container without a network; a taken-over API cannot read it and cannot
  get a server or CA certificate signed;
- every certificate that exists before the upgrade keeps logging in until it expires or is revoked.

## Decisions taken

| Question | Decision |
|----------|----------|
| Who generates a user's key | The API, server side; it returns a password-protected `.p12` once |
| Certificates per user | Several active ones, each listed and revoked on its own; renewal can overlap |
| Certificates from before the upgrade | Kept as `legacy` rows (hashes only); login keeps working; they can only be blocked in the API, never put on a CRL |
| Upload of a certificate file | Kept, as a second way to add a certificate to a user |
| Who issues | The app, as part of its administration features (not scripts) |
| Trust | Two chains: a new client CA trusted only by nginx (never installed on clients), and a public server certificate |
| Server certificate | Let's Encrypt with the DNS-01 challenge, renewed by certbot on the host; clients install nothing |
| Where the client CA key lives | A separate container `pki-signer`: no network, a Unix socket only, its own uid, key in a file |
| Signing service | Our own small stateless signer built on `cryptography` 45.0.7 (not step-ca, not `openssl ca`) |
| Revocation reaching nginx | CRL file plus automatic `nginx -s reload` through a systemd path unit; daily re-signing, 7 days validity |
| Files on disk | Each issued certificate also gets a directory `<store_dir>/<user_id>/<certificate_id>/` with `.crt`, `.p12`, `password.txt`, `info.txt`; no loose key |
| Re-download of a stored package | Only with the system permission `certificate.export` (built-in `admin`); counted and audited |
| Bulk issuing | None; one certificate per request |
| Revoking | Final: never archived, restored or deleted |
| Certificate state | `active` / `expired` / `revoked`, computed on read, never stored |
| Health | Reports CA expiry, the earliest CRL `next_update`, CRL stale, signer reachable; never whether PKI is configured |

Rejected, with the reason:

- One root for everything (the scripts today): every post machine trusts it for every HTTPS site, so whoever reads
  its key can impersonate any website to them. The two chains remove that risk instead of guarding it.
- step-ca in place of our signer: worth it only with the key on a YubiKey or HSM, or for a CA serving other systems.
  With a key file it adds a second source of truth for revocations (its database) and a second CRL to sign by hand
  (nginx checks a CRL at every level and step-ca signs with an intermediate).
- A separate PKI web application (EJBCA and the like): a second login and a second permission model for the same
  administrators, and user synchronisation; who may issue for whom is exactly the authorization v2 scope model.
- `openssl ca` with an index directory: two sources of truth, file locking, parsing `openssl` output.
- OCSP: every new TLS connection would depend on the API answering.

## Today

- Certificates are made by `gencert.sh` / `create_cert.sh` from one root (`rootCA.crt`, `rootCA.key`) that signs
  every user certificate and the server certificate; the root is installed as trusted on every client.
- The administrator uploads the user's `.crt` with `POST /users/`; the API stores only three hashes on the user row
  (`pki_certificate_cn_hash`, `_serial_hash`, `_fingerprint_hash`), one certificate per user.
- Login (`authenticate_pki`, `auth/service.py`) matches the hashes of the client certificate nginx verified; a session
  is bound to the certificate fingerprint (`certificate_fingerprint_hash`, `certificate_matches`).
- No list of issued serials exists, so no CRL can be produced; revoking means deactivating the user.

## Out of scope

- Hardware key storage (YubiKey, HSM, TPM); revisit with step-ca if wanted.
- An intermediate CA, OCSP, phones and tablets, bulk issuing.
- Putting `legacy` certificates on a CRL through the API (only hashes exist). The offline `sign-crl` command can list
  serials of the old root by hand (section 8).
- Replacing the client CA (every certificate issued again).
- Issuing server certificates: certbot does it.

## 1. Trust: two chains

**The client CA (new, online).**

- Created once by `python -m app.backend.signer init`: self-signed, CN `watch_web client CA` (configurable),
  RSA 4096, SHA-256, 10 years by default.
- Extensions: `basicConstraints CA:TRUE, pathlen:0` (critical), `keyUsage keyCertSign, cRLSign` (critical),
  `extendedKeyUsage clientAuth`, subject key identifier. The EKU on the CA limits everything below it to client
  authentication where verifiers apply it (Windows, Firefox): even a machine made to trust this CA could not accept a
  website certificate from it.
- One level: the signer signs user certificates with it directly, so nginx needs one CRL for it, signed by the
  signer automatically.
- Its certificate is held by nginx (`ssl_client_certificate`), the API (`ca_cert_path`: `ca_issued` on upload, the
  authority route) and the signer. It is never installed on a client machine. Its key is only in
  `PKI_DIR/private/`, which only the signer mounts.

**The server certificate (public).**

- Let's Encrypt with the DNS-01 challenge for a name under a domain you own; the server itself stays internal (the
  internal DNS or the hosts files point the name at the server's internal address). Clients use the name, never the IP.
- certbot on the host obtains and renews it (`--deploy-hook "nginx -t && systemctl reload nginx"`).
- Certificate Transparency logs publish every name in the certificate; a wildcard certificate (`*.<domain>`) keeps the
  host name out of them.
- Clients trust it through their normal trust store: nothing is installed on them.

**The existing root during the transition.**

- `ssl_client_certificate` is a bundle of the new client CA and the existing root, so `legacy` certificates keep
  working.
- With `ssl_crl` set nginx needs a CRL for every CA it trusts for clients, or it refuses every certificate. The
  existing root gets one CRL, signed once with its key by the offline command `sign-crl`, valid until the last old
  certificate expires; after that the old key goes offline. nginx reads one CRL file, so the API appends that CRL to
  the file it publishes (`settings.pki.extra_crl_paths`).
- After the server certificate switches to Let's Encrypt, the existing root is removed from every client's trust
  store (`deploy/pki/remove-old-root.cmd`): login does not need it there (live check 1, section 11).
- When the last old certificate has expired or been revoked, the existing root leaves the bundle and its CRL leaves
  `extra_crl_paths`.

**What a stolen secret gives.**

| Stolen | Gives | Does not give |
|--------|-------|---------------|
| Client CA key | Forged login certificates for watch_web (until noticed and the CA replaced) | Any website certificate: no client trusts the CA, and its EKU is `clientAuth` |
| The API (process or account) | Client certificates from the signer, the database | The CA key; a server or CA certificate; it already decided who logs in |
| `store_dir` | The `.p12` and password of issued certificates: logging in as those people | Anything else; 0700, API uid only |
| The existing root key | Until it leaves the clients: impersonating any website to them | Hence removed from clients early in the rollout, key offline |

## 2. The signing service `pki-signer`

**Container.**

- Same image as `api`, its own service in `docker-compose.yml` with `network_mode: none` and
  `command: ["python", "-m", "app.backend.signer", "serve"]`. It does not use the shared `x-app` block: it gets
  `env_file: signer.env`, not `.env`, so the database password and the secret key never reach it and the CA key
  password never reaches `api`.
- Runs as `${PKI_SIGNER_UID}:${MEDIA_GID}`. `private/client-ca.key` is 0400 owned by `PKI_SIGNER_UID`, so the API's
  uid cannot read it even on the host.
- Listens only on a Unix socket, `PKI_DIR/run/signer.sock` (0660; `run/` is 0770, group `MEDIA_GID`), served by
  uvicorn `--uds`. Only `api` also mounts `run/`. The socket's permissions are the authentication: no port, no token.
- Settings from `signer.env` (`SignerSettings`, its own small pydantic-settings class with the prefix `SIGNER_`; it
  never loads the app's `Settings`):

| Setting | Meaning | Default |
|---------|---------|---------|
| `SIGNER_CA_CERT_PATH` / `SIGNER_CA_KEY_PATH` | the client CA certificate and key (PEM) | required |
| `SIGNER_CA_KEY_PASSWORD` | only if the key is encrypted | empty |
| `SIGNER_SOCKET_PATH` | the socket to listen on | required |
| `SIGNER_MAX_VALIDITY_DAYS` | upper limit for a user certificate, checked again whatever the API sent | 730 |
| `SIGNER_MAX_CRL_DAYS` | upper limit for a CRL's `next_update` | 31 |

- At start it refuses to run when: the certificate is not a CA (`basicConstraints CA:TRUE`), the key does not match
  the certificate, `keyUsage` is present without `keyCertSign` and `cRLSign`, or the key is encrypted and the
  password is missing or wrong.

**Routes** (a small FastAPI app of its own, no OpenAPI document; paths in a `SignerRoutes` enum in
`enums/routes.py` like every other path):

| Route | Takes | Returns |
|-------|-------|---------|
| `POST /sign/client` | `public_key_pem`, `label`, `validity_days` | `certificate_pem`, `serial` (hex) |
| `POST /sign/crl` | `crl_number`, `this_update`, `next_update`, `entries` (`serial`, `revoked_at`, `reason`) | `crl_pem` |
| `GET /health` | | CA subject, `not_after`, SHA-256 fingerprint |

- The signer builds the whole certificate itself from the fixed profile of section 4; a request cannot add or change
  an extension, the subject beyond the label, or the issuer. It accepts only RSA public keys of 2048 to 4096 bits,
  labels of 1..64 characters and `validity_days` of 1..`SIGNER_MAX_VALIDITY_DAYS`; a CRL only with
  `next_update` <= now + `SIGNER_MAX_CRL_DAYS`.
- A refusal is 422 with a problem body (codes in `SignerErrorCode`); the API validated the same values first, so a
  refusal reaching the API is a 500 `internal_error` there.
- It never sees a user's private key: the API sends only the public key.
- Every signature is one log line of the signer (serial, CN, `not_after`, fingerprint; for a CRL its number and the
  number of entries): a record outside the database the API cannot change. Loguru with `diagnose=False`.
- It keeps no state. The database stays the single source of truth: the API holds the CRL number, builds the entries
  and writes the CRL file.

**Code boundaries.**

- `app/backend/pki/`: the library, no database and no FastAPI: the client certificate profile, CA checks, key and
  `.p12` generation, the password, the CRL builder, writing a certificate directory.
- `app/backend/signer/`: the service (`app.py`), its settings and its CLI (`__main__.py`: `serve`, `init`,
  `sign-crl`). It imports only `pki/`, `enums/` and `errors/`; never `models`, `database`, `services`, `auth`,
  `filters` or `api` (a test fails otherwise).
- The API reaches the signer only through `services/signer_client.py` (httpx over the Unix socket, 10 s timeout).
  No answer is `CodedError(ErrorCode.CERTIFICATE_AUTHORITY_UNAVAILABLE)`.
- `pki/` joins the packages that never reach FastAPI or Starlette (`tests/test_route_registry.py`). The signer's
  FastAPI app is not part of any published version and has no OpenAPI document.

## 3. API configuration and the folder

**Settings**, a new `settings.pki` (`MYSETTINGS_PKI__*`):

| Setting | Meaning | Default |
|---------|---------|---------|
| `ca_cert_path` | the client CA certificate (PEM), read-only | empty |
| `signer_socket` | the signer's Unix socket | empty |
| `store_dir` | the folder holding every issued certificate | empty |
| `crl_path` | the CRL file nginx reads (`ssl_crl`), written by the API | empty |
| `extra_crl_paths` | CRL files appended to the published file (the existing root's) | `[]` |
| `validity_days` / `max_validity_days` | default and upper limit for a new certificate | 365 / 730 |
| `key_size` | RSA key size of user keys | 2048 |
| `p12_encryption` | `aes256` (AES-256 with SHA-256), or `legacy` (3DES with SHA-1) for old Windows and macOS | `aes256` |
| `crl_days` | how long a signed CRL stays valid (`next_update`) | 7 |

- PKI is configured when `ca_cert_path`, `signer_socket`, `store_dir` and `crl_path` are all set. When only some are
  set the API refuses to start.
- Not configured: the API works as today plus the certificate list. Issue and export answer 503
  `certificate_authority_not_configured`; upload works (`ca_issued` is false); revoke works as a block in the API.
- Configured: at start the API refuses to run when `ca_cert_path` is not a readable CA certificate, `store_dir` or
  the directory of `crl_path` is not writable, or a file of `extra_crl_paths` does not parse as a CRL. The signer is
  not required for the API to start.
- Windows development without Docker: PKI stays unconfigured; the tests run the signer in-process.

**`PKI_DIR`** (host directory, bind-mounted at the same absolute path):

```
PKI_DIR/
  ca/client-ca.crt        0644  api (ca_cert_path), signer, nginx bundle
  ca/legacy-root.crt      0644  the existing rootCA.crt, while old certificates live
  ca/nginx-client-cas.pem 0644  client-ca.crt + legacy-root.crt (nginx ssl_client_certificate)
  private/client-ca.key   0400  PKI_SIGNER_UID only
  crl/nginx.crl           0644  published by the api (crl_path, nginx ssl_crl)
  crl/legacy-root.crl     0644  signed once offline (extra_crl_paths)
  issued/                 0700  api uid only (store_dir)
  run/                    0770  signer socket, group MEDIA_GID
```

- Mounts: `pki-signer` gets `ca/` (read-only), `private/` (read-only) and `run/`; `api` gets `ca/` (read-only),
  `crl/`, `issued/` and `run/`; `media-worker` and `imgproxy` get nothing of `PKI_DIR`.
- The CRL lives outside `store_dir`: `store_dir` holds `.p12` files and passwords (0700), nginx must read the CRL.

**Folder layout**, one directory per issued certificate, written once and never rewritten:

```
<store_dir>/<user_id>/<certificate_id>/
    certificate.crt
    certificate.p12     (0600)
    password.txt        (0600)
    info.txt            label, username at issue time, user id, issued at / by, serial, expires
```

- Paths use primary keys: usernames can change, ids do not. `info.txt` keeps the username for browsing by hand.
- The row records its directory relative to `store_dir` (`files_dir`).
- A directory removed by hand makes a re-download answer 410 `certificate_files_missing`; nothing else breaks.

## 4. Data and migration

**Table `user_certificates`** (model `UserCertificate`, `models/user_certificate.py`):

| Column | Notes |
|--------|-------|
| `id`, `user_id` (FK, index) | |
| `source` | `CertificateSource` (`enums/general.py`, no DB CHECK): `issued` (signed by the signer), `uploaded` (a file from an administrator or `init_user`), `legacy` (from before the upgrade), `configured` (hashes from `settings.admin`, the development seed) |
| `label` | the readable name (CN and friendly name); empty for `legacy` and `configured` |
| `subject_cn`, `serial` (hex), `not_before`, `not_after` | plain values; empty for `legacy` and `configured`, which have only hashes |
| `fingerprint_hash` (unique), `cn_hash`, `cn_hash_scheme`, `serial_hash` | the hashes login matches today (`auth/certificates.py`), filled for every row |
| `ca_issued` | signed by our client CA (`issued`, or an `uploaded` file whose signature verifies against `ca_cert_path`): it goes on our CRL when revoked |
| `revoked_at`, `revoked_by` (FK users), `revocation_reason` | `RevocationReason` (RFC 5280): `unspecified`, `key_compromise`, `affiliation_changed`, `superseded`, `cessation_of_operation` |
| `files_dir` | relative path of its directory; only for `issued` |
| `export_count` (default 0), `last_exported_at` | re-downloads |
| `created_at`, `created_by` (FK users, nullable) | |

Indexes: `user_id`, unique `fingerprint_hash`, `(ca_issued, revoked_at)` for the CRL.

**Table `certificate_authority_state`** (model `CertificateAuthorityState`), one row (`id = 1`, inserted by the
migration): `crl_number` (0), `this_update`, `next_update`, `revoked_count`, `published_at`.

**State** (`CertificateState`): `revoked` when `revoked_at` is set, else `expired` when `not_after` is in the past,
else `active`. A row without `not_after` (`legacy`, `configured`) is `active` until revoked. Computed in Python
(`certificate_state`) and in SQL for filters (`state_clause()`), never stored.

**Revoking is final.** No archive, restore or delete: a serial on a CRL can never be taken off safely. Revoked
certificates stay in the list, filterable by state. Archiving or deactivating a user does not revoke their
certificates (login already refuses the user); restoring the user brings them back.

**Migration `024_user_certificates`:**

- creates both tables (listed in `TABLES_OWNED_BY_LATER_REVISIONS` of `002_sync_schema_with_models.py`, and guarded
  with an inspector check) and inserts the state row;
- copies each user's three hashes and `pki_certificate_cn_hash_scheme` into a `legacy` row (`created_at` = the
  user's `created_at`, `created_by` null);
- drops the four `pki_certificate_*` columns of `users` with their indexes.

Downgrade:

- restores the columns; a user with a `legacy` row gets its values back exactly;
- a user without one gets the hashes of their newest active row; a user with none, or whose CN or serial hash
  another user's restored row already holds (the columns are unique), gets a placeholder (`unassigned:<user_id>`)
  that matches no certificate; the downgrade logs every such user;
- drops both tables. Issued certificates, revocations and exports are lost; the downgrade logs their count.

Up and down are tested exactly in the suite (sqlite) and checked live on MySQL 8 and MariaDB 11, like 021-023.

**Sessions** keep `certificate_fingerprint_hash` as their link to a certificate. New
`SessionRevokeCause.CERTIFICATE_REVOKED`: revoking a certificate ends the live sessions bound to it in the same
transaction (`AuthService.invalidate_certificate_sessions(..., cause=CERTIFICATE_REVOKED)`); the user's other
devices stay logged in. `CERTIFICATE_CHANGED` stays in the enum for existing rows; nothing writes it any more
(`PATCH /users/{id}` no longer takes a certificate).

## 5. Issuing and the package

**The certificate profile** (built by the signer, `pki/profile.py`):

| Field | Value |
|-------|-------|
| Subject | `CN=<label>` only; UTF-8, so Cyrillic works; 1..64 characters (RFC 5280 upper bound) |
| Issuer | the client CA's subject |
| Serial | random 159 bits (`x509.random_serial_number()`) |
| Validity | from 5 minutes ago (clock drift) to now + `validity_days` |
| Key | RSA `key_size`, exponent 65537, generated by the API |
| Extensions | `basicConstraints CA:FALSE` (critical), `keyUsage digitalSignature` (critical), `extendedKeyUsage clientAuth`, subject and authority key identifiers; no SAN |
| Signature | SHA-256 with the client CA key |

`clientAuth` is what makes browsers offer the certificate in the picker.

**The label** is required, for example `Petrenko I. - post 2 - laptop`: 1..64 characters after trimming, no control
characters (field error `certificate_label_invalid`). It becomes the CN and the `.p12` friendly name, so Chrome,
Edge, Firefox and the Windows store all show it. The web client may suggest one from the person's display name and
post. Login matches the fingerprint, never the label text.

**The `.p12`** holds the user's key and certificate under the label as friendly name, and no CA certificate (the
Windows import wizard could offer to install a CA as trusted). Encryption per `p12_encryption`.

**The password**: 20 characters in four groups of 5 joined by `-` (`k7Pm2-xQ9rT-...`), from an alphabet without
look-alikes (no `0 O 1 l I`), about 115 bits, from `secrets`.

**One issue request**, one transaction:

1. Lock the user row; an archived user answers 404 `user_not_found`; an inactive user may receive a certificate.
2. Generate the key; ask the signer for the certificate (503 `certificate_authority_unavailable` if it does not
   answer).
3. Insert the row and flush it for its id.
4. Write `<user_id>/<id>/` through a `.tmp-` directory renamed into place; directory 0700, `.p12` and
   `password.txt` 0600.
5. Write the audit row (`issue`): label, serial, SHA-256 fingerprint, `not_after`; never a key, `.p12` or password.
6. Commit. On any failure the directory is removed and the error raised again.

**The response** (201, `Cache-Control: no-store`):

```
{ "certificate": { ...the record... },
  "p12_base64": "...", "password": "...", "filename": "<label-ascii>.p12" }
```

`filename`: the label's ASCII letters and digits, other characters as `-`, or `certificate-<id>` when nothing is left.

**Export** (`certificate.export`) returns the same JSON from the folder, increments `export_count`, sets
`last_exported_at` and writes an audit row (`export`). Only for an `issued` certificate (409
`certificate_not_exportable` otherwise) that is `active` (409 `certificate_not_active`) and whose folder exists (410
`certificate_files_missing`). It does not need the signer.

**Upload** (`POST /users/{user_id}/certificates/upload`, multipart: `file` + `label`): parsed as today
(`CertificateParser`, size and emptiness checks), stored as `uploaded` with its plain values and hashes; `ca_issued`
when its signature verifies against the client CA. The same fingerprint already registered (any user) answers 400
`certificate_taken`. An already expired file is accepted and listed as `expired`.

## 6. API, permissions and errors

**New permissions** (`Permissions`, `PERMISSION_META` with Ukrainian display names, levels):

| Permission | Level | Allows |
|------------|-------|--------|
| `certificate.view` | scoped | seeing a user's certificates |
| `certificate.create` | scoped | issuing or uploading a certificate for a user |
| `certificate.revoke` | scoped | revoking a certificate |
| `certificate.export` | system | re-downloading a stored package |

- `admin` gets all four through its base (every permission). `POST_ADMIN_BASE` gains view, create and revoke. No
  other type's ceiling allows certificate permissions; `certificate.export` is a system permission, so only a
  holder at `/` has it.

**Checks** (scope first, then existence, then state, as the assignment routes):

- Routes on one user: the user is looked up and checked as `GET /users/{id}` does (`check_user_view`), then
  `certificate.view` must cover the user (`auth.covers_user`, else 403 through `access_denied`).
- Write routes: `require(all=[CERTIFICATE_VIEW, CERTIFICATE_X])`, the user check above, then
  `check_target_user(session, auth, user, Permissions.CERTIFICATE_X)` (covers the user, and the caller could grant
  every assignment of the user).
- A user does not see their own certificates through these routes without `certificate.view`; `/auth/me` shows the
  certificate of the current session.
- A certificate of another user than the one in the path is 404 `certificate_not_found`.
- `GET /certificates/` lists the certificates of non-archived users whose home post is where both `certificate.view`
  and `user.view` are in effect (a user without a home post: in effect at `/`).

**Routes** (enum members first; `UserRoutes` gains the five user members, a new `CertificateRoutes` group with
`BASE = "/certificates"`, `ROOT = "/"`, `AUTHORITY = "/authority"`; handlers in `api/v1/user_certificates.py`
(prefix `UserRoutes.BASE`, published after `users`) and `api/v1/certificates.py`):

| Route | Permissions | Purpose |
|-------|-------------|---------|
| `GET /users/{user_id}/certificates` | view | the user's certificates, filter `state` |
| `POST /users/{user_id}/certificates` | view + create | issue: `{label, validity_days?}`, 201 with the package |
| `POST /users/{user_id}/certificates/upload` | view + create | register a file (multipart `file` + `label`), 201 with the record |
| `GET /users/{user_id}/certificates/{certificate_id}` | view | one certificate |
| `POST /users/{user_id}/certificates/{certificate_id}/revoke` | view + revoke | `{reason}` (default `unspecified`); ends the sessions of that certificate |
| `POST /users/{user_id}/certificates/{certificate_id}/export` | view + export | re-download the package; POST because it is counted and audited |
| `GET /certificates/` | view | across users: filters `state`, `source`, `user_id`, `post_id`, `expires_within_days`; sort `not_after`, `created_at`, `label`, `id`; paginated |
| `GET /certificates/authority` | any logged-in user (`require()`) | the client CA: subject, `not_after`, SHA-256 fingerprint, PEM; CRL `this_update`, `next_update`, `revoked_count` |

The literal `upload` path is registered before `/{certificate_id}`. The list query model is
`api/v1/models/query/certificates.py` (`CertificateFilterParams`), converted to `CertificateCriteria(ListCriteria)`
next to `filters/certificate_filter.py` with its `SORTABLE_FIELDS`. A certificate record (`CertificateResponse`)
carries every column of section 4 but the hashes and `files_dir`, plus `state` and `has_package`.

**Changes to existing routes and tools:**

- `POST /users/`: the certificate file becomes optional (the form stays multipart), plus an optional
  `certificate_label` (default: the certificate's CN, cut to 64 characters). Without a certificate the user exists
  but cannot log in until one is issued or uploaded.
- `PATCH /users/{id}`: loses `certificate`. Replacing one is "issue or upload a new one, revoke the old one".
- `GET /auth/me`: gains `certificate` (`id`, `label`, `not_after`, `state`) of the current session, null for a
  session without one (development login), so the web client can warn "your certificate expires in N days".
- `python -m app.backend.cli.init_user`: the certificate path becomes optional; with one it creates an `uploaded`
  row (`--label`, default the CN).

These break the web client's contract: `api-contract/v1/openapi.json` is regenerated and the CHANGELOG gets a
migration note, as for authorization v2.

**New problem codes** (`enums/errors.py`):

| Code | Status | Class | When |
|------|--------|-------|------|
| `certificate_not_found` | 404 | `ErrorCode` | no such certificate for that user |
| `certificate_authority_not_configured` | 503 | `ErrorCode` | issue, export or `GET /certificates/authority` without PKI configured |
| `certificate_authority_unavailable` | 503 | `ErrorCode` | the signer did not answer |
| `certificate_revoked` | 401 | `AuthErrorCode` | login with a revoked certificate |
| `certificate_already_revoked` | 409 | `ErrorCode` | revoking twice |
| `certificate_not_active` | 409 | `ErrorCode` | export of a revoked or expired certificate |
| `certificate_not_exportable` | 409 | `ErrorCode` | export of a certificate the app did not issue |
| `certificate_files_missing` | 410 | `ErrorCode` | its folder was removed by hand |
| `certificate_label_invalid` | field | `FieldErrorCode` | the label |

Reused: `certificate_taken` (400; its title becomes "Certificate already registered", any user),
`certificate_invalid`, `certificate_file_empty`, `certificate_file_too_large`, `user_not_found`. A `validity_days`
outside 1..`max_validity_days` is the range field error inside 422 `validation_error`. Every route declares its
problems with `problems(...)`.

## 7. Revocation, CRL, nginx, login, health

**Revoking** (one transaction): lock the row; 409 `certificate_already_revoked` if revoked; set `revoked_at`,
`revoked_by`, `revocation_reason`; end the sessions bound to it (`CERTIFICATE_REVOKED`); audit row (`revoke`);
commit. Then, for a `ca_issued` certificate with PKI configured, publish the CRL. Publishing after the commit means
the CRL never lists what the database rolled back. A failed publish is logged; the revocation holds in the API and
the health check reports the CRL stale. A `legacy`, `configured` or foreign `uploaded` certificate is blocked in the
API only.

**Publishing** (`services/crl_publisher.py`):

1. Lock the state row (`FOR UPDATE`; on sqlite a no-op), so two API processes never overwrite a newer CRL with an
   older one. When the caller is the startup, re-check inside the lock whether the file is still missing or past
   half of its validity (another worker may have published meanwhile); a revocation and `pki crl` always publish.
2. `crl_number + 1`; entries: every `ca_issued` certificate that is revoked and not yet expired (an expired one may
   leave the CRL, RFC 5280), each with its reason code; `this_update` now, `next_update` now + `crl_days`.
3. The signer signs it (extensions `CRLNumber` and `AuthorityKeyIdentifier`, SHA-256). No answer: roll back, the
   number is not consumed, `CodedError(CERTIFICATE_AUTHORITY_UNAVAILABLE)`.
4. File content: the signed CRL, then each file of `extra_crl_paths` (read at every publish). Written as
   `crl_path.tmp` in the same directory, then `os.replace`, so nginx never reads half a file.
5. Update `this_update`, `next_update`, `revoked_count`, `published_at`; commit.

**Freshness.** The CRL is re-signed:

- on every revocation of a `ca_issued` certificate;
- daily by `python -m app.backend.cli.pki crl`, run by a systemd timer (`docker compose exec -T api ...`);
- at API start when the file is missing or half of its validity has passed (skipped with a warning when the signer
  does not answer).

After re-signing the existing root's CRL by hand, `pki crl` puts it into the published file.

**nginx** (`nginx.conf`, paths and comments):

```
ssl_certificate        /etc/letsencrypt/live/<name>/fullchain.pem;
ssl_certificate_key    /etc/letsencrypt/live/<name>/privkey.pem;
ssl_client_certificate <PKI_DIR>/ca/nginx-client-cas.pem;   # client CA + existing root while old certificates live
ssl_crl                <PKI_DIR>/crl/nginx.crl;             # published by the API; every CA above needs its CRL in it
ssl_verify_client      on;
ssl_verify_depth       1;                                   # user certificate -> its CA
```

**systemd** (`deploy/systemd/`, paths edited by the operator):

- `watch-web-crl.path` (`PathChanged=` on `crl_path`) starts `watch-web-crl.service`:
  `nginx -t && nginx -s reload`. If the live check shows the rename does not trigger it, the unit watches the
  directory instead.
- `watch-web-crl-refresh.timer` (daily, `Persistent=true`) starts `watch-web-crl-refresh.service`, which runs
  `pki crl` in the api container.

**Health** (`/health`, only when PKI is configured; nothing about whether it is configured): `pki` with
`ca_expires_in_days`, `crl_next_update_in_days` (the earliest `next_update` of every CRL in the published file, so
the existing root's CRL cannot run out unnoticed), `crl_stale` (a `ca_issued` revocation newer than `published_at`)
and `signer_available`. Less than 2 days left, a stale CRL or no signer report as degraded.

**Login:**

- `authenticate_pki` matches the three hashes against `user_certificates` instead of `users`, so `legacy` rows match
  exactly as today.
- A revoked certificate answers 401 `certificate_revoked` (the person knows to ask for a new one); an expired one
  `certificate_not_recognized` (nginx refuses it first anyway).
- On every request (in `get_current_user`, one indexed query) and every refresh, the certificate a session is bound
  to must be an active row of that user; otherwise the session ends with `CERTIFICATE_REVOKED` and the request
  answers 401 `token_certificate_mismatch`, as today. `certificate_matches` keeps comparing the presented
  certificate with the bound one.
- The development login keeps its JSON body and matches against the same table.

## 8. CLI and the first administrator

**API side, `python -m app.backend.cli.pki`** (in the api container):

| Command | What it does |
|---------|--------------|
| `issue --username U --label L [--days N] --out DIR` | issues for an existing user exactly like the API (folder, audit row) and copies `.p12` + `password.txt` to `DIR` to hand over; `created_by` is null and the audit row has `via: cli` |
| `crl` | publishes the CRL now (the daily timer runs this) |
| `status` | client CA subject and expiry, CRL state, signer reachable, certificates active / expiring within 30 days / revoked |

**Signer side, `python -m app.backend.signer`:**

| Command | Where | What it does |
|---------|-------|--------------|
| `serve` | `pki-signer` container | the service |
| `init [--cn CN] [--days N]` | `docker compose run --rm pki-signer ...` | creates the client CA at `SIGNER_CA_CERT_PATH` / `SIGNER_CA_KEY_PATH` (key 0400, encrypted when `SIGNER_CA_KEY_PASSWORD` is set); refuses to overwrite |
| `sign-crl --ca-cert F --ca-key F [--key-password-file F] --next-update DATE --out F [--serial HEX ...]` | anywhere the key is (offline machine, or the signer image with the key mounted from a USB stick) | signs a CRL for another CA: the existing root's, empty or listing serials by hand |

**The first administrator:**

- Where the seed creates the admin user (development), it writes a `configured` row from `settings.admin` (or the
  generated credentials), as today but in the new table. `MYSETTINGS_ADMIN__PKI_CERTIFICATE_*` become optional.
- On a server with PKI: `init_user --username admin --user-type admin` without a certificate, then
  `pki issue --username admin --label ... --out DIR` once.

## 9. The guide: `PKI.md`

Linked from DEPLOYMENT.md:

1. **How it fits together:** the two chains, what each secret gives when stolen (the table of section 1).
2. **The server certificate:** a name under your domain, internal DNS or hosts files, certbot with the DNS plugin of
   your provider, the deploy hook, the CT logs note and the wildcard option.
3. **The client CA:** `signer init`, the `PKI_DIR` layout and permissions, `PKI_SIGNER_UID`, `.env` and
   `signer.env`, the compose mounts; the key never in git, an encrypted offline backup of `private/`.
4. **The existing root:** the bundle, `sign-crl` once with the old key (valid until the last old certificate
   expires, re-sign before that if needed), then the key offline; removing the root from clients
   (`deploy/pki/remove-old-root.cmd`: `certutil -delstore Root <thumbprint>` machine-wide or `-user`; Firefox by
   hand if it was imported there); retiring it from the bundle.
5. **Installing a person's `.p12`:** the Windows wizard step by step, Firefox "Your Certificates", how the label
   appears in the picker, choosing another certificate (Chrome remembers the choice until restart).
6. **nginx and systemd:** start order (signer and api first, the API publishes the CRL, then `nginx -t` and reload),
   installing the units, `systemctl enable --now watch-web-crl.path watch-web-crl-refresh.timer`.
7. **Checks:** `openssl s_client`, `curl --cert-type P12`, a revocation test (revoke a test certificate, watch
   nginx refuse it within seconds).
8. **Renewal:** `GET /certificates/?expires_within_days=30`, issue the new one, revoke the old one as `superseded`;
   the client CA's own expiry.
9. **Troubleshooting by browser error:** `ERR_CERT_COMMON_NAME_INVALID` (the name typed is not the certificate's),
   `ERR_BAD_SSL_CLIENT_AUTH_CERT` (client certificate refused: revoked, expired, or a CRL out of date), nginx 400
   "The SSL certificate error", every certificate refused at once (a CRL in the file passed its `next_update`).

## 10. Tests

All with a throwaway client CA (and a throwaway "existing root") generated in fixtures, never a real key.

- **`pki/`:** the certificate profile (extensions, CN, validity, serial size, no SAN); opening the `.p12` with its
  password, friendly name = label, both encryption modes, no CA certificate inside; the password alphabet and
  format; the CRL (entries, reasons, `CRLNumber`, the signature verifying against the CA, expired entries dropped);
  the CA checks.
- **The signer** (in-process through the ASGI transport): the fixed profile whatever the request carries; the limits
  (label, validity, key type and size, CRL days); refusing to start (not a CA, key mismatch, `keyUsage` without
  `cRLSign`, an encrypted key with and without its password); `init` and `sign-crl`; the import boundary.
- **The routes:** each route: success, the permission pair, delegation inside and outside a scope, the check order;
  every problem declared (the conftest check); export counter and audit row; files written once with 0700 / 0600
  (POSIX only, skipped on Windows); the folder removed when the transaction fails; 503 without PKI and with the
  signer down; an upload signed by the client CA getting `ca_issued`; revoking publishes a CRL with the extra CRLs
  appended; revoking with the signer down holds and the health reports the CRL stale; partial configuration refuses
  to start.
- **Login:** an issued certificate through the nginx headers and through the development JSON; a revoked one gives
  401 `certificate_revoked`; revoking ends that device's sessions but not the user's other devices; `legacy` and
  `configured` rows log in.
- **Migration `024`:** up and down exact in the suite; live on MySQL 8 and MariaDB 11.
- **Contract and configuration:** `openapi.json` regenerated, CHANGELOG migration note, route registry (with
  `SignerRoutes`), `tests/test_nginx_conf.py` (`ssl_crl`, the bundle, depth 1), a compose check (`pki-signer` has
  `network_mode: none` and `env_file: signer.env`; only it mounts `private/`; `media-worker` and `imgproxy` mount
  nothing of `PKI_DIR`).

## 11. Live checks

1. **First, before the rest is built on it:** Chrome, Edge and Firefox on Windows offer a client certificate whose CA
   the machine does not trust, and still do after the existing root is removed from the machine. Automated part: I
   prepare the certificates and the nginx container; the browser part is yours.
2. In an `nginx:1.27` container with a generated client CA, a generated "existing root" with its CRL, a server
   certificate, a `.p12` from each CA and the published CRL file: `curl` passes with both; after a revocation and a
   reload the revoked one is refused; with the existing root's CRL past `next_update` every certificate is refused
   (the documented risk).
3. The signer and the API over the real socket in the Docker e2e stack (issue, revoke, CRL file).
4. The systemd path unit in a container with systemd if that works; otherwise a step for your staging server.
5. Yours: importing a `.p12` in your browser and seeing the label in the picker.

## 12. Documents

`PKI.md`; a "PKI" section in CLAUDE.md with the rules for future work (and the topology line: four containers);
API.md (routes and codes); CHANGELOG; DEPLOYMENT.md (link, `PKI_DIR`, `PKI_SIGNER_UID`, order of steps);
`.env.example`, `signer.env.example`, `docker-compose.yml`, `nginx.conf`; `deploy/systemd/*`,
`deploy/pki/remove-old-root.cmd`.

## 13. Rollout on your server

1. Server certificate: certbot with DNS-01 for the chosen name, internal DNS, nginx `ssl_certificate` switched,
   reload. Independent of the app; can go first.
2. Deploy the new version: migration 024 turns current certificates into `legacy` rows; everyone keeps logging in.
3. Create `PKI_DIR`; `signer init`; `sign-crl` for the existing root with its key, then move that key offline; fill
   `.env` and `signer.env`; start `pki-signer`, restart `api`: the first CRL file appears.
4. nginx: `ssl_client_certificate` bundle, `ssl_crl`, `ssl_verify_depth 1`; `nginx -t`; reload.
5. Install and enable the systemd units.
6. Remove the existing root from every client (after live check 1 passed).
7. Issue new certificates in the web client. Old ones keep working until they expire or are revoked; then the
   existing root leaves the bundle and `extra_crl_paths`. Until then, re-sign its CRL before `next_update` (the
   health check warns).

## Code layout

New:

- `app/backend/pki/` (library), `app/backend/signer/` (service and CLI)
- `app/backend/models/user_certificate.py` (`UserCertificate`, `CertificateAuthorityState`)
- `app/backend/services/certificates.py` (issue, upload, revoke, export; returns dataclasses),
  `services/crl_publisher.py`, `services/signer_client.py`
- `app/backend/filters/certificate_filter.py` (`CertificateCriteria`)
- `app/backend/api/v1/user_certificates.py`, `api/v1/certificates.py`, and their models in
  `api/v1/models/{request,response,query}/certificates.py`
- `app/backend/cli/pki.py`
- `app/backend/migrations/versions/024_user_certificates.py`
- `deploy/systemd/watch-web-crl.{path,service}`, `deploy/systemd/watch-web-crl-refresh.{timer,service}`,
  `deploy/pki/remove-old-root.cmd`, `signer.env.example`, `PKI.md`

Changed:

- `enums/routes.py` (`UserRoutes`, `CertificateRoutes`, `SignerRoutes`), `enums/general.py` (`Permissions`,
  `CertificateSource`, `CertificateState`, `RevocationReason`, `SessionRevokeCause.CERTIFICATE_REVOKED`),
  `enums/errors.py`
- `bootstrap/permissions.py`, `auth/user_types.py`, `auth/service.py`, `auth/sessions.py`, `auth/dependencies.py`,
  `bootstrap/seed.py`, `bootstrap/startup.py`, `cli/init_user.py`, `config.py` (`PkiConfig`)
- `api/v1/users.py`, `api/v1/auth.py` and their models, `api/v1/__init__.py`, `api/system/health_checks.py`,
  `models/user.py`
- `docker-compose.yml`, `nginx.conf`, `.env.example`, `api-contract/v1/openapi.json`, API.md, DEPLOYMENT.md,
  CLAUDE.md, CHANGELOG.md
