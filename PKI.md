# Client certificates (PKI)

The application issues the client certificates people log in with, binds them to users and revokes them. This guide
covers the server side: the certificates, the folder, the containers, nginx and systemd, the checks and what to do
when a browser refuses. The API side (routes, problem codes, the `.p12` flow in the web client) is in API.md,
"Client certificates"; the order of the upgrade steps is in DEPLOYMENT.md, "Upgrading to PKI client certificates".

## 1. How it fits together

Two certificate chains, each trusted only where it is needed:

- **The client CA** signs the users' certificates. It is created once on the server (`signer init`), is trusted only
  by nginx (`ssl_client_certificate`) and is never installed on a client machine: browsers offer a client certificate
  whose CA the machine does not trust (checked with Firefox and Edge, ASCII and Cyrillic labels). The CA certificate
  carries `extendedKeyUsage clientAuth`, so even a machine made to trust it would not accept a website certificate
  from it where verifiers apply the EKU (Windows, Firefox).
- **The server certificate** is a public one (Let's Encrypt). Clients trust it through their normal trust store and
  install nothing.

```
  browser --443, client certificate--> nginx (host)
                                         | ssl_client_certificate PKI_DIR/ca/nginx-client-cas.pem
                                         | ssl_crl                PKI_DIR/crl/nginx.crl   <-- written by the api
                                         v
                                       [api container] --- PKI_DIR/run/signer.sock ---> [pki-signer container]
                                         | generates keys, stores            no network, own uid,
                                         | packages in PKI_DIR/issued/       the only holder of the CA key
                                         v                                   (PKI_DIR/private/)
                                       MySQL: user_certificates, the CRL number

  systemd: watch-web-crl.path      PKI_DIR/crl/nginx.crl changed -> nginx -t && nginx -s reload
           watch-web-crl-refresh   daily: docker compose exec -T api python -m app.backend.cli.pki crl
```

- The API generates each user's key, sends only the public key to the signer, and returns the `.p12` with its
  password once. It keeps a copy of the package in `PKI_DIR/issued/<user_id>/<certificate_id>/` for a re-download
  by holders of `certificate.export` (a system permission).
- The signer (`python -m app.backend.signer serve`) signs exactly one certificate profile (CN = the label,
  `clientAuth`, no SAN) and CRLs. It keeps no state: the database is the single source of truth for certificates,
  revocations and the CRL number. Every signature is one line of its log (`docker compose logs pki-signer`).
- Revoking a certificate ends the sessions of that device at once. For a certificate this server issued the API then
  publishes a new CRL, the path unit reloads nginx, and nginx refuses the certificate within seconds; a `legacy`
  certificate or an uploaded one of another CA is blocked in the API only (no key here can put it on a CRL).

What a stolen secret gives:

| Stolen | Gives | Does not give |
|--------|-------|---------------|
| Client CA key (`PKI_DIR/private/`) | Forged login certificates for watch_web, until noticed and the CA replaced | Any website certificate: no client trusts the CA, and its EKU is `clientAuth` |
| The API (process or account) | Client certificates from the signer, the database | The CA key; a server or CA certificate; it already decided who logs in |
| `PKI_DIR/issued/` | The `.p12` and password of issued certificates: logging in as those people | Anything else; 0770 root:`PKI_SOCKET_GID`, which only the api container joins (never the nginx user, even when it is `MEDIA_UID`) |
| The existing root key | Until it leaves the clients: impersonating any website to them | Hence removed from clients early in the rollout, key offline |

## 2. The server certificate

1. Choose a name under a domain you own, for example `watch.example.org`. The server itself stays internal: the
   internal DNS (or the hosts file of each client) points the name at the server's internal address. Clients always
   use the name, never the IP: the certificate holds names only.
2. Obtain the certificate on the host with certbot and the DNS-01 plugin of your DNS provider (the server needs no
   inbound access from the internet, only outbound HTTPS to Let's Encrypt and the provider's API):

   ```bash
   sudo certbot certonly --dns-<provider> --dns-<provider>-credentials /root/.secrets/<provider>.ini \
     -d watch.example.org \
     --deploy-hook "nginx -t && systemctl reload nginx"
   ```

   The distribution's certbot timer renews it; the deploy hook reloads nginx after every renewal.
3. Point `nginx.conf` at it (`ssl_certificate /etc/letsencrypt/live/watch.example.org/fullchain.pem`,
   `ssl_certificate_key .../privkey.pem`), then `sudo nginx -t && sudo systemctl reload nginx`.

Certificate Transparency logs publish every name in a public certificate. A wildcard certificate
(`-d '*.example.org'`, DNS-01 only) keeps the host name out of them.

This step does not depend on the application and can go first (DEPLOYMENT.md, step 1 of the upgrade).

## 3. The client CA, `PKI_DIR` and the signer

### The folder

`PKI_DIR` is a host directory bind-mounted at the same absolute path into the containers. It must live outside the
project directory: the image is built from the project directory (`build: .`), and a `PKI_DIR` inside it would put
the CA key and the issued packages into the build context.

| Path | Owner | Mode | Used by |
|------|-------|------|---------|
| `PKI_DIR/` | root | 0755 | |
| `ca/` | `PKI_SIGNER_UID` | 0755 | `signer init` writes `client-ca.crt` here |
| `ca/client-ca.crt` | `PKI_SIGNER_UID` | 0644 | api (`ca_cert_path`), signer, nginx bundle |
| `ca/legacy-root.crt` | root | 0644 | the existing `rootCA.crt`, while old certificates live (section 4) |
| `ca/nginx-client-cas.pem` | root | 0644 | `client-ca.crt` + `legacy-root.crt` (nginx `ssl_client_certificate`) |
| `private/` | `PKI_SIGNER_UID` | 0700 | the signer only |
| `private/client-ca.key` | `PKI_SIGNER_UID` | 0400 | the signer only (written 0400 by `signer init`) |
| `crl/` | `MEDIA_UID` | 0755 | written by the api, read by nginx |
| `crl/nginx.crl` | `MEDIA_UID` | 0644 | published by the api (`crl_path`, nginx `ssl_crl`) |
| `crl/legacy-root.crl` | root | 0644 | signed offline for the existing root (`extra_crl_paths`) |
| `issued/` | root, group `PKI_SOCKET_GID` | 0770 | `store_dir`: every issued package; inside, 0700 directories of the api's uid, `.p12` and `password.txt` 0600 |
| `run/` | `PKI_SIGNER_UID`, group `PKI_SOCKET_GID` | 0770 | the signer's socket `signer.sock`, 0660, group `PKI_SOCKET_GID` |

`MEDIA_UID` / `MEDIA_GID` are the uid and gid the api container runs as (`.env`, DEPLOYMENT.md "Media storage").
`PKI_SIGNER_UID` is a uid of its own, so the api's uid cannot read the CA key, not even on the host. The socket has
no token and no port: whoever can open it can have client certificates signed, so its permissions are the
authentication. It is 0660 in `run/` (0770), both of the group `PKI_SOCKET_GID`: a group of its own with no host
account in it. The signer runs with it as its group, and the api container joins it (`group_add` in
`docker-compose.pki.yml`); only the api container mounts `run/`. It is not `MEDIA_GID`: that group is often shared
with host accounts (the nginx worker user, when `MEDIA_UID` is `www-data`), which must never reach the signer.
`issued/` belongs to the same group for the same reason: the api writes its packages there as a member of
`PKI_SOCKET_GID`, and a host account, even the one with the uid `MEDIA_UID`, cannot open the folder (the per-user
folders inside are 0700 of `MEDIA_UID`, but the way to them goes through `issued/`).

As root on the host:

```bash
PKI_DIR=/srv/watch_web/pki                       # outside the project directory
MEDIA_UID=10001 MEDIA_GID=10001                   # the values of .env
useradd --system --no-create-home --shell /usr/sbin/nologin watch-web-signer
groupadd --system watch-web-pki                   # the socket's group: add no account to it
PKI_SIGNER_UID=$(id -u watch-web-signer)
PKI_SOCKET_GID=$(getent group watch-web-pki | cut -d: -f3)

install -d -m 0755 "$PKI_DIR"
install -d -m 0755 -o "$PKI_SIGNER_UID" -g "$PKI_SOCKET_GID" "$PKI_DIR/ca"
install -d -m 0700 -o "$PKI_SIGNER_UID" -g "$PKI_SOCKET_GID" "$PKI_DIR/private"
install -d -m 0755 -o "$MEDIA_UID" -g "$MEDIA_GID" "$PKI_DIR/crl"
install -d -m 0770 -o root -g "$PKI_SOCKET_GID" "$PKI_DIR/issued"      # not MEDIA_UID (section 1)
install -d -m 0770 -o "$PKI_SIGNER_UID" -g "$PKI_SOCKET_GID" "$PKI_DIR/run"
```

### `.env` and `signer.env`

The PKI part of compose is an override file, `docker-compose.pki.yml` (the `pki-signer` service, the api's
`MYSETTINGS_PKI__*` and its `PKI_DIR` mounts), off by default so `docker-compose.yml` runs without `PKI_DIR`. Enable
it in `.env` (next to `docker-compose.yml`):

```bash
COMPOSE_FILE=docker-compose.yml:docker-compose.pki.yml
PKI_DIR=/srv/watch_web/pki
PKI_SIGNER_UID=<id -u watch-web-signer>
PKI_SOCKET_GID=<getent group watch-web-pki | cut -d: -f3>
# the existing root's CRL, while its certificates live (section 4)
MYSETTINGS_PKI__EXTRA_CRL_PATHS=["/srv/watch_web/pki/crl/legacy-root.crl"]
```

Compose sets `MYSETTINGS_PKI__CA_CERT_PATH`, `SIGNER_SOCKET`, `STORE_DIR` and `CRL_PATH` from `PKI_DIR`; do not set
them in `.env`. The other settings (`VALIDITY_DAYS` 365, `MAX_VALIDITY_DAYS` 730, `KEY_SIZE` 2048, `P12_ENCRYPTION`
`aes256` or `legacy`, `CRL_DAYS` 7) are listed in `.env.example`. PKI is configured when all four paths are set; the
API refuses to start with only some of them, with a CA certificate it cannot read, with a folder it cannot write or
with an extra CRL that does not parse. It also refuses `KEY_SIZE` outside 2048..4096, `CRL_DAYS` or `VALIDITY_DAYS`
below 1 and `VALIDITY_DAYS` above `MAX_VALIDITY_DAYS`.

The signer keeps its own limits (`signer.env`, below), which the API does not see: keep
`MYSETTINGS_PKI__MAX_VALIDITY_DAYS` at most `SIGNER_MAX_VALIDITY_DAYS` and `MYSETTINGS_PKI__CRL_DAYS` at most
`SIGNER_MAX_CRL_DAYS`. Otherwise the signer refuses: an issue asking for more days answers 500, and every CRL
publication fails (logged, `/health` reports the CRL stale and then expiring).

The signer never reads `.env` (the database password and the secret key never reach it) and the api never reads
`signer.env` (the CA key password never reaches it):

```bash
cp signer.env.example signer.env        # replace /srv/watch_web/pki with PKI_DIR
chmod 0600 signer.env
```

| `signer.env` | Meaning | Default |
|--------------|---------|---------|
| `SIGNER_CA_CERT_PATH` / `SIGNER_CA_KEY_PATH` | the client CA certificate and key (PEM) | required |
| `SIGNER_CA_KEY_PASSWORD` | password of the key; set it before `init` to have the key written encrypted | empty |
| `SIGNER_SOCKET_PATH` | the socket to listen on (`PKI_DIR/run/signer.sock`) | required |
| `SIGNER_MAX_VALIDITY_DAYS` | longest validity the signer signs, whatever the API sent | 730 |
| `SIGNER_MAX_CRL_DAYS` | latest `next_update` of a CRL it signs | 31 |

`signer.env` and `.env` are ignored by git and by the image build (`.gitignore`, `.dockerignore`).

The compose mounts:

| Container | Mounts of `PKI_DIR` |
|-----------|---------------------|
| `pki-signer` | `ca/` (read-only), `private/` (read-only), `run/`; `network_mode: none`, `env_file: signer.env`, user `PKI_SIGNER_UID:PKI_SOCKET_GID` |
| `api` | `ca/` (read-only), `crl/`, `issued/`, `run/`; user `MEDIA_UID:MEDIA_GID` with `group_add: PKI_SOCKET_GID` |
| `media-worker`, `imgproxy` | nothing |

The `pki-signer` service uses the image `watch-web` built by the api service (`pull_policy: never`); on an
air-gapped server `docker load` it like the api image.

### Creating the client CA

Once, from the directory with `docker-compose.yml`. The service itself mounts `ca/` and `private/` read-only, so
`docker compose run pki-signer ... init` cannot write the files (`Read-only file system`); `init` runs in a one-off
container that mounts them writable:

```bash
docker run --rm --network none --user "$PKI_SIGNER_UID:$PKI_SOCKET_GID" --env-file signer.env \
  -v "$PKI_DIR/ca:$PKI_DIR/ca" -v "$PKI_DIR/private:$PKI_DIR/private" \
  watch-web python -m app.backend.signer init --cn "watch_web client CA"
# signer: client CA written to /srv/watch_web/pki/ca/client-ca.crt and /srv/watch_web/pki/private/client-ca.key
```

- RSA 4096, SHA-256, 10 years (`--days N` to change), `basicConstraints CA:TRUE, pathlen:0`, `keyUsage keyCertSign,
  cRLSign`, `extendedKeyUsage clientAuth`. `--cn` is the issuer name browsers show next to each certificate.
- It refuses to overwrite an existing certificate or key (`signer: a CA file already exists, nothing written`).
- The key never goes into git or into the project directory. Keep one encrypted copy of `private/` offline (for
  example `tar -C "$PKI_DIR" -cz private | gpg --symmetric > client-ca-key.tgz.gpg` on a USB stick in a safe). Losing
  the key means a new client CA and a new certificate for everyone; a copy in the wrong hands means forged logins.

Then start the signer and restart the api. With `MYSETTINGS_PKI__EXTRA_CRL_PATHS` set, sign the existing root's CRL
first (section 4, steps 1 and 2): the API refuses to start with an extra CRL it cannot read.

```bash
docker compose up -d pki-signer
docker compose up -d api
docker compose logs api | grep -i crl          # CRL published: number=1, revoked=0 / CRL published at start
docker compose exec api python -m app.backend.cli.pki status
```

The API does not wait for the signer: it starts without it, answers 503 `certificate_authority_unavailable` on
issue until the signer is there, and publishes the CRL at start when the file is missing or half of its validity has
passed (skipped with a warning when the signer does not answer). The signer refuses to start when the certificate is
not a CA (`not_a_ca`), its `keyUsage` lacks `keyCertSign` or `cRLSign` (`key_usage`), the key does not match
(`key_mismatch`), the files cannot be read (`unreadable`) or the key password is missing or wrong (`key_password`).

### The first administrator

On a server with PKI the first administrator gets a certificate from the command line (the development seed's
`MYSETTINGS_ADMIN__PKI_CERTIFICATE_*` does not apply in production):

```bash
docker compose exec api python -m app.backend.cli.init_user --username admin --alias "Administrator" --user-type system_administrator
docker compose exec api python -m app.backend.cli.pki issue --username admin --label "Admin - office PC" --out /tmp/admin-cert
install -d -m 0700 /root/watch-web-admin-cert                      # outside the project directory
docker compose cp api:/tmp/admin-cert/. /root/watch-web-admin-cert/
docker compose exec api rm -r /tmp/admin-cert
# after the hand-over (section 5):
shred -u /root/watch-web-admin-cert/*.p12 /root/watch-web-admin-cert/password.txt && rmdir /root/watch-web-admin-cert
```

`pki issue` issues exactly as the API does (folder, audit row with `via: cli`, `created_by` null) and writes
`<label>.p12` and `password.txt` (0600) into `--out`, a new or empty directory inside the container. Copy them out
to a directory only root reads and outside the project directory: anything under the project directory lands in the
image build context and may end up in git. Hand both over (section 5), then destroy that copy (`shred -u`, or `rm`
when the directory is on a tmpfs such as `/run`); the server keeps its own copy in `PKI_DIR/issued/` for a
re-download. Everyone else gets their certificate from the web client.

## 4. The existing root during the transition

Certificates made by `gencert.sh` / `create_cert.sh` from `rootCA.crt` keep logging in: migration 024 turned each
user's certificate into a `legacy` row with the same hashes. nginx must keep accepting them until they expire or are
replaced.

1. Copy the root certificate (never its key) into `ca/` and build the bundle nginx trusts:

   ```bash
   install -m 0644 rootCA.crt "$PKI_DIR/ca/legacy-root.crt"
   cat "$PKI_DIR/ca/client-ca.crt" "$PKI_DIR/ca/legacy-root.crt" > "$PKI_DIR/ca/nginx-client-cas.pem"
   chmod 0644 "$PKI_DIR/ca/nginx-client-cas.pem"
   ```

2. Sign one CRL for the root with its key, valid until the last old certificate expires (find the dates with
   `openssl x509 -noout -enddate -in <user>.crt` in the folders the scripts made). nginx checks every certificate
   against the CRL of its issuer: a CA in the bundle without a valid CRL in `ssl_crl` has every one of its
   certificates refused (checked live: the other CA's certificates keep working). `sign-crl` runs wherever the key
   is: on an offline machine with the application installed, or in the image with the key mounted read-only from a
   USB stick (as root, so it can read a key file only root reads):

   ```bash
   docker run --rm --network none --user 0:0 \
     -v /media/usb/old-root:/old:ro -v "$PKI_DIR/crl:$PKI_DIR/crl" \
     watch-web python -m app.backend.signer sign-crl --ca-cert /old/rootCA.crt --ca-key /old/rootCA.key \
       --next-update 2027-06-30 --number 1 --out "$PKI_DIR/crl/legacy-root.crl"
   # signer: CRL number 1 with 0 entries written to /srv/watch_web/pki/crl/legacy-root.crl
   ```

   - `--next-update YYYY-MM-DD` must be in the future; `--number` goes up by one every time it is signed again;
     `--key-password-file F` for an encrypted key; `--serial HEX ...` lists old certificates as revoked by hand (the
     API can only block a `legacy` certificate, it has only its hashes).
   - Put it into `MYSETTINGS_PKI__EXTRA_CRL_PATHS` (section 3) and restart the api: the published file is the CRL of
     the client CA followed by the CRLs of every extra file, read and parsed again at every publication (only the
     CRLs are written, nothing else of the file). When an extra file is missing or does not parse then, nothing is
     published: the previous file stays, the api logs `CRL not published: pki.extra_crl_paths: ...`, `/health`
     shows a revocation since then as `crl_stale` and turns `degraded` as the file's `next_update` nears.
     `sign-crl` writes `--out` through a temporary file it renames, so a publication never reads half a file.
   - After signing it again by hand, run `docker compose exec api python -m app.backend.cli.pki crl` so the published
     file picks it up. `GET /health` reports `crl_next_update_in_days` as the earliest `next_update` of every CRL in
     the file and turns `degraded` below 2 days: sign it again before that.

3. Move the old key offline: no copy of `rootCA.key` stays on the server or on any machine that is online.
4. Remove the root from every client once the server certificate is the public one (section 2), so no client trusts
   it for websites any more. Login does not need it there. `deploy/pki/remove-old-root.cmd` runs
   `certutil -delstore Root <thumbprint>` for the machine store (as administrator) or, with `user`, for the current
   user's store:

   ```
   remove-old-root.cmd <thumbprint>          (machine store, from an administrator prompt)
   remove-old-root.cmd <thumbprint> user     (store of the current user)
   ```

   It takes exactly 40 hexadecimal characters and nothing else (exit code 1 with its usage otherwise): certutil also
   accepts an index, a serial number or a name there, and would delete whatever certificate matches it.

   The thumbprint is the SHA-1 of the certificate itself: `openssl x509 -in rootCA.crt -noout -fingerprint -sha1`
   without the colons, or the line `Cert Hash(sha1)` of `certutil -dump rootCA.crt`. (`certutil -hashfile` hashes the
   file's bytes: the thumbprint only for a DER file, not for PEM.) Firefox keeps its own store: where the root was
   imported there, delete it in Settings > Privacy & Security > Certificates > View Certificates > Authorities.
5. Retire it when no old certificate is in use any more. `legacy` rows have no expiry date (only hashes), so they stay
   `active` in the list until revoked: revoke the remaining ones (`GET /api/v1/certificates/?source=legacy&state=active`,
   reason `superseded` or `cessation_of_operation`; the last active certificate of the last effective administrator
   answers 409 `last_admin_required`, API.md "The last administrator": issue theirs first); files of the old root
   uploaded after the upgrade (`source` `uploaded`, `ca_issued` false) expire on their own. Then in one change: the
   bundle becomes `client-ca.crt` alone,
   `MYSETTINGS_PKI__EXTRA_CRL_PATHS` goes, restart the api, `pki crl`, `nginx -t && nginx -s reload`, delete
   `ca/legacy-root.crt` and `crl/legacy-root.crl`.

## 5. Installing a person's `.p12`

The web client shows the password once and saves the `.p12` under the suggested file name. Hand the file and the
password over separately (for example the file on a USB stick, the password on paper or by phone). The `.p12` holds
the key and the certificate under the label as friendly name, and no CA certificate, so no import wizard offers to
install a CA as trusted. Nothing else is installed on the client.

**Windows (Chrome and Edge use the Windows store):**

1. Double-click the `.p12`. The Certificate Import Wizard opens; Store Location: **Current User**; Next.
2. File to Import: the `.p12` is already filled in; Next.
3. Private key protection: type the password. Leave "Mark this key as exportable" off (the server keeps the package
   for a re-download) and "Include all extended properties" on; "Enable strong private key protection" asks for
   confirmation at every use, leave it off unless wanted. Next.
4. Certificate Store: **Automatically select the certificate store based on the type of certificate** (it lands in
   Personal); Next, Finish: "The import was successful".
5. Check: `certmgr.msc` > Personal > Certificates shows the label under "Issued To" and the client CA's name under
   "Issued By". Its General tab says Windows cannot verify the certificate: expected, the CA is not installed, and
   login works anyway.

**Firefox (its own store):** Settings > Privacy & Security > Certificates > View Certificates > Your Certificates >
Import, pick the `.p12`, type the password (Firefox asks for its primary password first when one is set).

**The picker.** On the first visit the browser asks which certificate to present: the label is the name shown
(Chrome and Edge: "Select a certificate" with the label and the issuer; Firefox: "User Identification Request" with
the label and the serial). Cyrillic labels show as written. Login matches the certificate's fingerprint, never the
label text.

**Choosing another certificate.** A browser remembers the choice for the site, "no certificate" included (then nginx
answers 400 "No required SSL certificate was sent" on every reload):

- Firefox: Settings > Privacy & Security > Certificates > View Certificates > Authentication Decisions, delete the
  site's entry (or use a private window, or restart Firefox).
- Chrome and Edge: until the browser is restarted completely (every window closed, no background process left).

Old Windows and macOS versions cannot open AES-256 packages and report a wrong password: set
`MYSETTINGS_PKI__P12_ENCRYPTION=legacy` (3DES with SHA-1) for packages issued from then on.

## 6. nginx and systemd

`nginx.conf` (DEPLOYMENT.md, "Host nginx"), with `/srv/watch_web/pki` replaced by `PKI_DIR`:

```
ssl_certificate        /etc/letsencrypt/live/<name>/fullchain.pem;
ssl_certificate_key    /etc/letsencrypt/live/<name>/privkey.pem;
ssl_client_certificate <PKI_DIR>/ca/nginx-client-cas.pem;   # client CA + existing root while old certificates live
ssl_crl                <PKI_DIR>/crl/nginx.crl;             # published by the API; every CA above needs its CRL in it
ssl_verify_client      on;
ssl_verify_depth       1;                                   # user certificate -> its CA
```

Start order: the signer and the api first; the api publishes `crl/nginx.crl` at start. nginx refuses a configuration
whose `ssl_crl` file does not exist (`nginx -t`: `cannot load CRL ... No such file or directory`), so check that the
file is there, then `sudo nginx -t && sudo systemctl reload nginx`. The nginx master runs as root and reads `ca/` and
`crl/` at every start and reload; the files are 0644.

nginx reads the CRL only when it starts or reloads. The units in `deploy/systemd/` keep it current:

| Unit | What it does |
|------|--------------|
| `watch-web-crl.path` | `PathChanged=` on `crl/nginx.crl`: starts `watch-web-crl.service` whenever the API publishes |
| `watch-web-crl.service` | `nginx -t && nginx -s reload` (a configuration that fails `nginx -t` is never loaded; the old CRL stays) |
| `watch-web-crl-refresh.timer` | daily (`Persistent=true`, up to an hour of random delay): starts the refresh service |
| `watch-web-crl-refresh.service` | `docker compose exec -T api python -m app.backend.cli.pki crl` in the directory with `docker-compose.yml` |

```bash
sudo cp deploy/systemd/watch-web-crl.* deploy/systemd/watch-web-crl-refresh.* /etc/systemd/system/
sudo editor /etc/systemd/system/watch-web-crl.path               # PathChanged=<PKI_DIR>/crl/nginx.crl
sudo editor /etc/systemd/system/watch-web-crl-refresh.service    # WorkingDirectory=<directory with docker-compose.yml>
sudo systemctl daemon-reload
sudo systemctl enable --now watch-web-crl.path watch-web-crl-refresh.timer
systemctl list-timers watch-web-crl-refresh.timer
```

- The API writes `nginx.crl.tmp` and renames it over `nginx.crl`; the rename triggers the path unit (checked with
  systemd 252: every publication started one reload, and a publication during a running reload was covered by that
  reload, which reads the newest file). If on your host the unit does not start after a publication (section 7),
  watch the directory instead: `PathChanged=<PKI_DIR>/crl/`.
- A CRL is valid `crl_days` (7) and signed again every day, at every revocation of a certificate this server issued,
  and at API start when half of its validity has passed: a timer or signer outage has at least six days before nginx
  refuses the new certificates (`GET /health` is `degraded` from 2 days before). A publication that fails is logged
  and changes nothing else: the revocation stands, the API starts, the previous file stays.

## 7. Checks

On the host (`/health` answers only on loopback in production; `--resolve` keeps the name the certificate holds):

```bash
# TLS: the public chain, and the CAs nginx accepts client certificates from
openssl s_client -connect 127.0.0.1:443 -servername watch.example.org </dev/null 2>/dev/null \
  | grep -E "Verify return code|Acceptable client certificate CA names" -A2

# A .p12 passes; no certificate gets nginx's 400
curl -sS --resolve watch.example.org:443:127.0.0.1 --cert-type P12 --cert test.p12:'<password>' \
  https://watch.example.org/health
curl -sS --resolve watch.example.org:443:127.0.0.1 https://watch.example.org/health | grep -o "<title>.*</title>"
# <title>400 No required SSL certificate was sent</title>

# The CA, the CRL, the signer, the counts
docker compose exec api python -m app.backend.cli.pki status
```

Revocation test (do it once after the setup, with a test user):

1. Issue a certificate for the test user in the web client, check `curl --cert-type P12 ...` answers `/health`.
2. Revoke it. `docker compose logs api | grep "CRL published"` shows the next number, and
   `journalctl -u watch-web-crl.service -n 5` shows `Finished watch-web-crl.service` within seconds.
3. The same `curl` now gets `<title>400 The SSL certificate error</title>`; every other certificate still passes.

systemd on the staging server (the path unit was checked in a container; confirm it on the real host once):
`docker compose exec -T api python -m app.backend.cli.pki crl`, then `journalctl -u watch-web-crl.service -n 5` must
show a start after the publication. If it does not, switch to watching the directory (section 6) and repeat.

## 8. Renewal

- Certificates about to expire: `GET /api/v1/certificates/?state=active&expires_within_days=30` (the web client's
  list), or the count in `pki status`.
- Renew by issuing a new certificate for the same user (several active ones may overlap), handing it over, and, once
  the person logged in with it, revoking the old one with the reason `superseded`. Nothing is renewed in place.
- `GET /auth/me` carries the certificate of the session (`not_after`, `state`): the web client warns the person.
- The client CA lives 10 years (`ca_expires_in_days` in `/health`). A certificate issued near its end outlives it and
  stops working when the CA expires. Replacing the client CA means a new CA and a new certificate for everyone; plan
  it well ahead.
- The existing root's CRL: sign it again before its `next_update` while old certificates live (section 4).

## 9. Troubleshooting

| Browser shows | Cause | Fix |
|---------------|-------|-----|
| `ERR_CERT_COMMON_NAME_INVALID` (Chrome, Edge), `SSL_ERROR_BAD_CERT_DOMAIN` (Firefox) | The name typed is not the certificate's: an IP address, or another name | Use the name of the server certificate; internal DNS or hosts file |
| `NET::ERR_CERT_AUTHORITY_INVALID` | The server still presents a certificate of the existing root, and the root was removed from this client | Switch nginx to the public certificate first (section 2) |
| nginx page "400 No required SSL certificate was sent" | No certificate installed, or the browser remembered "no certificate" | Install the `.p12` (section 5); forget the choice (section 5) |
| nginx page "400 The SSL certificate error", or `ERR_BAD_SSL_CLIENT_AUTH_CERT` | The client certificate was refused: revoked, expired, or its CA has no valid CRL in the file | Issue a new certificate; check `pki status` and `/health` |
| Every certificate of one CA refused at once | That CA's CRL in `crl/nginx.crl` passed its `next_update` (or is missing): the client CA's when the CRL was not signed for `crl_days` (signer down, timer stopped); the existing root's when its CRL ran out | `/health` `crl_next_update_in_days` below 0. Start the signer and run `pki crl`; for the existing root sign its CRL again (section 4), then `pki crl` |
| The API answers 401 `certificate_revoked` at login | Revoked. A `legacy` certificate, or an uploaded one of another CA, is blocked in the API only (nginx lets it through); one this server issued means nginx still has the previous CRL | Issue a new certificate. For one issued here: the path unit did not reload nginx, `journalctl -u watch-web-crl.service`, `nginx -t` |
| The API answers 401 `certificate_not_recognized` | The certificate is not registered for any user, or expired | Issue one, or upload the file for the user |
| The web client is logged out after a revocation (401 `session_expired`) | Revoking ends the sessions of that certificate | Log in with another certificate |
| 503 `certificate_authority_unavailable` on issue | The signer does not answer, or the api cannot open its socket | `docker compose ps pki-signer`, `docker compose logs pki-signer`; `PKI_SOCKET_GID` in `.env`, owner and group of `run/` (section 3) |
| 503 `certificate_authority_unavailable` and the api log line `Signer protocol mismatch: the api speaks 1, the signer supports [2]. Fix: run one release tag for api and pki-signer (deploy/release.sh status).` | api and pki-signer run different releases (a container started by hand): every request names its protocol version in `X-Signer-Protocol` and the signer refuses any other (409 `protocol_unsupported`) | `deploy/release.sh status` shows the image of each service; `docker compose up -d` puts both on `WATCH_WEB_TAG` (RELEASE.md) |
| `pki-signer` unhealthy in `docker compose ps` | Its health check (`GET /health` over its socket, `python -m app.backend.signer check` by hand) gets no answer | `docker compose logs pki-signer`; the CA files and `signer.env` (section 3) |
| 503 `certificate_authority_not_configured` | `settings.pki` is not set in the api | `COMPOSE_FILE` in `.env` (section 3), restart the api |
| 500 `internal_error` on issue, the api logs `signer answered 422 ... validity_days` | The request asked for more days than `SIGNER_MAX_VALIDITY_DAYS` (`MYSETTINGS_PKI__MAX_VALIDITY_DAYS` is above it) | Lower `MAX_VALIDITY_DAYS` or raise the signer's limit (section 3) |
| `/health` `crl_stale` true, the api logs `CRL not published` | A publication failed: the signer does not answer or refuses (`signer answered 422 ... crl_dates`: `CRL_DAYS` above `SIGNER_MAX_CRL_DAYS`), an extra CRL is missing or does not parse, or `crl/` is not writable. A revocation stands all the same, and the API starts | Fix what the log line names, then `docker compose exec api python -m app.backend.cli.pki crl` |
| The api does not start: `PKI is partly configured`, `not a usable CA certificate`, `not a writable directory`, `extra_crl_paths ... is not a readable CRL file` | `settings.pki` points at something missing | Fix the path or the owner the message names |
| The api does not start: `validation error for PkiConfig` | `KEY_SIZE`, `CRL_DAYS`, `VALIDITY_DAYS` or `MAX_VALIDITY_DAYS` out of range (section 3) | Fix the value the message names |
| The signer does not start: `signer: the CA is not usable: <reason>` | See section 3, "Creating the client CA" | `signer.env` paths, `SIGNER_CA_KEY_PASSWORD`, owner of `private/` |
| Windows import: "The password you entered is incorrect" with the right password | An old Windows or macOS that cannot open AES-256 packages | `MYSETTINGS_PKI__P12_ENCRYPTION=legacy`, issue again |
