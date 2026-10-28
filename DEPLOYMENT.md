# Deployment

## Topology

Docker runs the application only. Everything stateful stays outside the containers.

```
                     +----------------------- host --------------------------+
  clients  --443-->  |  nginx (entry point, TLS, upload offload, media,     |
                     |         thumbnail cache /var/cache/nginx/thumbs)      |
                     |     |  proxy 127.0.0.1:8000        |  /_thumb/,       |
                     |     |                              |  /media-images/  |
                     |     v                              v                  |
                     |  [api container]            [imgproxy container]      |
                     |  [media-worker container]                             |
                     |     |  127.0.0.1:3306    \  PKI_DIR/run/signer.sock   |
                     |     v                     \-> [pki-signer container]  |
                     |  MySQL (host service)         (no network)            |
                     |                                                       |
                     |  MEDIA_ROOT  (local disk, or NFS/SMB share mounted    |
                     |               on the host) bind-mounted at the same   |
                     |               path into api, media-worker, imgproxy   |
                     |  PKI_DIR     (client CA, CRL, issued certificates)    |
                     |               bind-mounted into api and pki-signer    |
                     +-------------------------------------------------------+
```

- Containers use the host network, so the database, nginx and imgproxy are all reached on `127.0.0.1` and nothing on the host needs to be reconfigured. A bridge-network variant is described at the bottom of `docker-compose.yml`. The exception is `pki-signer` (client certificates, `docker-compose.pki.yml`): it has no network at all and answers only on a Unix socket; the guide is `PKI.md`.
- The media directory is mounted at the same absolute path inside and outside the containers. The database stores paths relative to it, so the directory can move as long as `MEDIA_ROOT` and the nginx `alias` move with it.
- The API applies migrations on start and seeds the admin user; the worker only needs the database and the media directory.

## Media storage

Any filesystem mounted on the host works: a local disk, an NFS export, an SMB share, or a block volume from the cloud. Mount it on the host, point `MEDIA_ROOT` at it, and give the mount to the containers through `docker-compose.yml` (already done with `${MEDIA_ROOT}:${MEDIA_ROOT}`).

What the pipeline needs from the filesystem:

| Need | Why |
|------|-----|
| POSIX rename on the same filesystem | uploads are moved from `MEDIA_ROOT/_tmp` into place; posters are written as `.tmp-xxxx` then renamed, storyboards as a `.tmp-` directory renamed into place |
| Readable by the nginx worker user | nginx serves originals with `alias`; imgproxy reads originals and posters |
| Writable by `MEDIA_UID:MEDIA_GID` | the API writes uploads, the worker writes `MEDIA_ROOT/_derived/posters/` and `MEDIA_ROOT/_derived/storyboards/` |
| Reasonable read latency | imgproxy reads the whole original on a cache miss; the nginx cache keeps that to once per thumbnail |

Notes for network shares:

- NFS: mount with `hard,intr` (or `hard` on modern kernels); keep the nginx cache on local disk, never on the share.
- SMB/CIFS: mount with `uid=MEDIA_UID,gid=MEDIA_GID,file_mode=0644,dir_mode=0755` so the containers and nginx agree on ownership. Rename is atomic on CIFS.
- Object storage (S3 and similar) is not a filesystem. Mount it with rclone or s3fs on the host if it must be used; native S3 support (imgproxy can read `s3://` sources, the API and nginx cannot yet) is a separate change.

Ownership: run `id -u <owner>` and `id -g <owner>` of `MEDIA_ROOT` and put the numbers in `MEDIA_UID` / `MEDIA_GID`. The containers run as that user. nginx only reads, so it needs group or world read (`chmod -R g+rX` or `o+rX`). The uid does not need an account inside the image: the image sets `USER`, `LOGNAME` and `HOME` so libraries that look up the current user keep working.

## Host nginx

Install the four files on the host:

```bash
sudo cp nginx-http.conf /etc/nginx/conf.d/00-watch-web-http.conf     # cache zone, upstreams (http context)
sudo mkdir -p /etc/nginx/snippets
sudo cp nginx-app.conf /etc/nginx/snippets/watch-web-app.conf         # locations (server context)
sudo cp nginx-proxy.conf /etc/nginx/snippets/watch-web-proxy.conf     # API proxy headers, certificate headers
sudo cp nginx.conf /etc/nginx/sites-available/watch_web               # server blocks: 80 -> 443, TLS + client certificates
sudo ln -s /etc/nginx/sites-available/watch_web /etc/nginx/sites-enabled/
sudo mkdir -p /var/www/nginx_uploads /var/cache/nginx/thumbs /var/cache/nginx/stream_auth
sudo chown www-data:www-data /var/www/nginx_uploads /var/cache/nginx/thumbs /var/cache/nginx/stream_auth
sudo chmod 755 /var/cache/nginx      # the workers (www-data) reach thumbs/ and stream_auth/ through it; at 0700 every thumbnail answers 200 with 0 bytes
sudo nginx -t && sudo systemctl reload nginx
```

Edit before reloading:

| In file | Line | Set to |
|---------|------|--------|
| nginx-app.conf | `alias /var/www/radio_monitoring/uploads/;` | `MEDIA_ROOT` with a trailing slash |
| nginx-app.conf | `root /var/www/radio_monitoring/uploads;` (stream location) | `MEDIA_ROOT` without a trailing slash: the same directory as the alias, or every seek of a player asks the API twice |
| nginx-app.conf | `client_body_temp_path /var/www/nginx_uploads 1 2;` | `NGINX_UPLOAD_TEMP` |
| nginx.conf | `server_name` (both servers) | your domain |
| nginx.conf | `ssl_certificate` / `ssl_certificate_key` | the server certificate (with its chain) and key: Let's Encrypt's `fullchain.pem` / `privkey.pem` for your name (PKI.md, section 2) |
| nginx.conf | `ssl_client_certificate` / `ssl_crl` | `PKI_DIR/ca/nginx-client-cas.pem` (the client CA, plus the existing root while old certificates live) and `PKI_DIR/crl/nginx.crl` (published by the API) (PKI.md, sections 3, 4 and 6) |
| nginx.conf | `root /var/www/watch_web_client;` in `location /` | the web client build (`index.html` and assets) |
| nginx-http.conf | `server 127.0.0.1:8000;` / `server 127.0.0.1:8080;` | `API_PORT` / `IMGPROXY_PORT` |
| nginx-http.conf | `proxy_cache_path /var/cache/nginx/thumbs ... max_size=20g` | local disk, sized for the box |
| nginx-http.conf | `proxy_cache_path /var/cache/nginx/stream_auth ... max_size=100m` | small as it is: one entry per token and video, kept 30 s (API.md, "Video and audio in a player") |
| nginx-app.conf | `client_max_body_size 65m;` in the `/api/v1/media/uploads` location | above `storage.upload.max_chunk_bytes`, never equal: the API answers an oversized chunk itself (`upload_chunk_too_large`) |
| nginx-app.conf | `client_max_body_size 11G;` (server level) | above `storage.max_file_size_gb`, never equal: the API answers an oversized file itself (`media_file_too_large`) |

The API container mounts `NGINX_UPLOAD_TEMP` at the same path, so the temp file nginx names in `X-File-Name` is the file the API moves. The directory must be writable by nginx and readable by `MEDIA_UID`; the simplest arrangement is `MEDIA_UID` = the nginx worker user (`www-data`). With PKI that is why nothing secret is owned by `MEDIA_UID`: `PKI_DIR/issued/` (the packages) belongs to root and the group `PKI_SOCKET_GID`, which only the api container joins (PKI.md, section 3).

If another nginx or a load balancer sits in front of this host, keep this server block as is and pass TLS through to it (TCP, no TLS termination in front): this nginx must see the client certificate. The cache and the internal locations must live on the nginx that can read `MEDIA_ROOT`.

nginx answers some requests itself, in its own format: no or an invalid client certificate, a body above its limits, the API not reachable or too slow, a file it cannot serve. Those are the infrastructure's errors; clients handle them by status (API.md, "Errors"). Every response carries `X-Request-ID` (nginx's `$request_id`): the access log `/var/log/nginx/watch_web.access.log` (format `watch_web` in nginx-http.conf; rotated with the other `/var/log/nginx/*.log` by the distribution's logrotate) records it, the API logs it on every line of the request and returns it as the `error_id` of a 500. `grep <id>` in both logs (`docker compose logs api`) finds both sides of a request.

### Web client and other clients

- The web client is served by this server block at `/` (`location /` in `nginx.conf`, its `root` is deployment configuration); `nginx-app.conf` claims only the paths the API publishes (`/api/`, the OpenAPI document and viewers of each version included, `/problems`, `/health`). Same origin, so the API sends no CORS headers and needs none.
- Integrations that call the API from their servers (another system's backend, scripts) need no CORS either: they connect with a client certificate issued by the CA in `ssl_client_certificate`, like users.
- A browser application on another origin is the only case for CORS. Add it at this nginx, not in the API, so nginx's own responses (413, 502, 504) carry the headers too: an allowlist `map $http_origin $cors_origin` in the http context, `add_header Access-Control-Allow-Origin $cors_origin always`, `Access-Control-Allow-Credentials true` (the browser must present the client certificate), `Access-Control-Expose-Headers` for `Location, Retry-After, Upload-Offset, Upload-Length, Upload-Expires, X-Thumbnail-Status, Content-Disposition, Accept-Post, WWW-Authenticate, X-Request-ID`, and `Vary: Origin`. Browsers send the CORS preflight (`OPTIONS`) without a client certificate, which `ssl_verify_client on` refuses: such an origin needs `ssl_verify_client optional` with the certificate enforced per location for everything but `OPTIONS`. Design it with the first concrete integration.

### Client certificates (mTLS)

nginx is the control point for user certificates. `ssl_verify_client on` makes every request (API, thumbnails, `/api/v1/docs`) present a certificate issued by `ssl_client_certificate`, inside its validity period and not on `ssl_crl`; anything else gets an nginx 400 page ("No required SSL certificate was sent" / "The SSL certificate error") and never reaches the API. Port 80 only redirects to 443.

- The API binds `127.0.0.1` (`MYSETTINGS_SERVER__HOST`, set by `docker-compose.yml`). With host networking `0.0.0.0` would publish the port: a client reaching it directly could send `X-SSL-Client-*` headers nginx never checked and log in with public certificate data. uvicorn trusts `X-Forwarded-For` only from `FORWARDED_ALLOW_IPS` (default `127.0.0.1`, where host nginx connects from; the image no longer passes `*`), and `nginx-proxy.conf` sets `X-Forwarded-For $remote_addr` (replaces what the client sent), so `request.client.host` is the real peer and a forged header is not believed.
- The API sees the certificate only through four headers nginx sets in `nginx-proxy.conf`: `X-SSL-Client-Verify`, `-DN`, `-Serial`, `-Fingerprint`. `POST /auth/login` maps them to a user. nginx replaces any header of these names the client sends, but only in locations that include the snippet: every location with `proxy_pass http://watch_web_api` must include it (`tests/test_nginx_conf.py` fails otherwise).
- Certificates are issued, listed and revoked in the application (PKI.md). Revoking one through the API ends its sessions at once and publishes a new CRL; `deploy/systemd/watch-web-crl.path` reloads nginx when the file changes, and nginx refuses the certificate from then on. A resumed TLS session is not checked again, for at most `ssl_session_timeout` (10 minutes). The CRL is signed again every day (`watch-web-crl-refresh.timer`) and is valid 7 days: a CRL past its `nextUpdate` makes nginx refuse every certificate of its CA (`/health` turns `degraded` 2 days before).
- `ssl_verify_depth 1`: a user certificate is signed directly by a CA of the bundle; there are no intermediates.
- Servers run with `MYSETTINGS_APP__ENVIRONMENT=production` (the Docker image sets it; `app/config.toml` says `development`, so a venv run must set it). The API refuses to start in production with an empty or placeholder `MYSETTINGS_SECURITY__SECRET_KEY`. In `development` the login takes the certificate fields from a JSON body without any check.
- Excel reports are built in memory (about 0.45 ms per band row end to end, database load included: 19 500 rows took 9 s on the test server; the workbook build alone is about 0.3 ms per row): `MYSETTINGS_REPORTS__MAX_ROWS` (default 20000) limits the band rows of one `POST /observations/report`; a larger result answers `422 report_too_large`. Raise it only together with the memory of the api container.
- The first administrator on a server: `init_user` without a certificate, then `python -m app.backend.cli.pki issue` for them (PKI.md, "The first administrator"). `MYSETTINGS_ADMIN__PKI_CERTIFICATE_CN` / `_SERIAL` / `_FINGERPRINT` only serve the development seed; credentials generated on first start have no private key anyone can install.
- `/health` is limited to loopback in production (`MYSETTINGS_HEALTH__ENABLE_HOST_CHECK` defaults to on there): run the check on the host (`curl --resolve <name>:443:127.0.0.1 --cert-type P12 --cert user.p12:'<password>' https://<name>/health`); from another machine it answers 403.
- Check: the same `curl` answers on the host; without `--cert` it gets 400 (PKI.md, section 7).

## MySQL on the host

The containers connect to `MYSETTINGS_DATABASE__HOST=127.0.0.1` over the host network, so MySQL only needs to listen on TCP `127.0.0.1:3306` (the default) and the application user needs `'radio_user'@'127.0.0.1'` or `'radio_user'@'localhost'` grants over TCP. A socket-only MySQL (`skip-networking`) does not work from a container.

## Database backups

`docker-compose.backup.yml` adds the `db-backup` tool service (`mariadb-dump` in the `watch-web` image, run by `deploy/systemd/watch-web-db-backup.timer`) and mounts `BACKUP_DIR` read-only into the api; `docker-compose.backup-copy.yml` adds an off-host copy on a mounted share. Setup, the restore runbook and what a rollback does not bring back: `BACKUPS.md`. The database user needs `ALL` on `` `radio\_monitoring\_%`.* `` (staging and kept schemas) and `PROCESS`.

## Application containers

```bash
cp .env.example .env        # fill: database password, secret key, IMGPROXY_KEY/SALT, MEDIA_ROOT, MEDIA_UID/GID; PKI (below)
docker compose build
docker compose up -d
docker compose ps           # api and imgproxy report healthy; media-worker and pki-signer have no health endpoint
docker compose logs -f media-worker
```

Client certificates add the `pki-signer` container and the api's PKI mounts through the override `docker-compose.pki.yml`, enabled in `.env` with `COMPOSE_FILE=docker-compose.yml:docker-compose.pki.yml`, `PKI_DIR` (a host directory outside the project directory, bind-mounted at the same path) `PKI_SIGNER_UID` (the uid that owns `PKI_DIR/private` and runs the signer) and `PKI_SOCKET_GID` (a group of its own for the signer's socket, 0660 in `PKI_DIR/run/`: the signer runs with it and the api container joins it; not `MEDIA_GID`, which host accounts such as the nginx user may share, and they must never reach the signer). Its own environment is `signer.env` (from `signer.env.example`, 0600). Without the override `docker-compose.yml` runs as it is and the API has no PKI. Setup: `PKI.md`; order of the upgrade: "Upgrading to PKI client certificates" below.

Key and salt: `python3 -c "import secrets; print(secrets.token_hex(32))"` twice. The same values go to `IMGPROXY_KEY` / `IMGPROXY_SALT` (imgproxy container) and `MYSETTINGS_STORAGE__IMGPROXY__KEY` / `SALT` (API). Rotating them invalidates every issued public thumbnail URL at once.

Air-gapped hosts: build on a connected machine, `docker save watch-web ghcr.io/imgproxy/imgproxy:v3 | gzip > images.tgz`, copy, `docker load`. Nothing is downloaded at runtime. `pki-signer` runs the same `watch-web` image and is never pulled (`pull_policy: never`).

Upgrades: back up first, `docker compose run --rm db-backup create --label pre-upgrade` (BACKUPS.md; it rolls back with `restore` if the upgrade goes wrong), then `docker compose build api && docker compose up -d api media-worker`. The API runs migrations on start; start the worker after the API has finished the first start. Upgrading to authorization v2: run the report on a copy first (see "Upgrading to authorization v2" below). Upgrading to PKI client certificates: the order of steps is in "Upgrading to PKI client certificates" below. Upgrading to storyboards with `MYSETTINGS_STORAGE__POSTER__CONCURRENCY=1`: see the upgrade note under "Storyboards" (the worker refuses to start until the concurrency is 2 or storyboards are off).

Upgrading to corps and brigades (migration 027): the API adds `scope_groups.kind` (every existing group becomes a plain group, `group`) and the table `observation_units` on start; nothing else to run. nginx needs no change (the routes live under `/api/`). Observations created before the upgrade have no recorded corps or brigade (none existed). Downgrade: `python -m app.backend.cli.migrations_manager downgrade 026_users_no_hot_columns` drops the column and the table: corps and brigades stay as plain scope groups with their posts, the recorded units are lost.

Filling the unit history of old observations: observations created before their post was put into a corps or a brigade have no recorded unit, so `placement=recorded` (the default) leaves them out. After the corps and brigades are set up and the posts placed, run `docker compose exec api python -m app.backend.cli.units fill-history --dry-run` first, then `docker compose exec api python -m app.backend.cli.units fill-history`. Only an observation (archived ones included) with no recorded unit at all gets one row for every unit its post is in at the run, with the observation's own `created_at`; an observation that already has a row (corps only, brigade only, or both) is history and is left as it is, rows that exist are never changed, and posts in no unit are skipped. Run it again after posts are placed later (only observations still without a unit are filled). It is safe while the API runs and idempotent: a second run fills nothing. Work is done per post in chunks of 1000 observations, one transaction each, so a run stopped by a database error keeps the chunks it already committed (they carry no audit row); run the command again and it continues, because it fills only what is still missing. It prints a `--dry-run` notice when nothing is written, one line per unit (`kind id name: observations filled`) and the totals (`observations`, `rows`, `posts in no unit`). A run that wrote anything leaves one audit row (`fill_history`, per-unit counts). Without a container: `venv/bin/python -m app.backend.cli.units fill-history`.

Upgrading to sizes as displayed (vertical phone videos, EXIF-rotated photos): rows stored before keep the stored frame size until probed again. `docker compose exec media-worker python -m app.backend.cli.media_tools probe-missing --all` probes every video and audio file again and re-reads every image size (headers only; `--limit N` per run, default 1000). Video uploads that failed only because they have no video stream become `unsupported` after `media_tools reconcile-posters --retry-failed`: the worker sees the probed metadata and stops trying.

Offloaded uploads (`/api/v1/media/nginx-upload`): nginx keeps the request body in `NGINX_UPLOAD_TEMP` (`client_body_in_file_only` never deletes it). The API accepts `X-File-Name` only for a regular file inside that directory (`storage.nginx_upload_temp_path`, same path as `client_body_temp_path`), moves it on success and deletes it on every other outcome once the path is checked. A request refused before the handler runs (an expired token, 401) leaves its file, so sweep the directory from cron (see "Scheduled cleanup" below): `docker compose exec api python -m app.backend.cli.media_tools sweep-nginx-temp` deletes files older than `storage.nginx_upload_temp_max_age_hours` (24 by default; `--max-age-hours N` overrides it). Keep the age above the longest upload, nginx may still be writing a younger file. A file the command cannot read or delete (permissions) is logged, left in place and counted (`skipped N file(s)` in its output); the sweep goes on with the others.

### Upgrading to authorization v2 (user types, scope groups, role assignments)

Migrations `021_scope_groups` (scope groups, `posts.group_id`), `022_builtin_roles` (`roles.builtin_key`; a custom
role named like a built-in key other than `admin` becomes `<key>_custom`) and `023_authorization_v2` convert every
user: roles, home post and access rows become role assignments (a role at the root or at a post) and a user type;
`admin.true` and `post.access_all` leave the catalog. Look at what the conversion will do on a copy first:

1. Copy the production database, for example:

   ```bash
   mysql -e "CREATE DATABASE radio_monitoring_copy CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
   mysqldump --single-transaction --routines --triggers radio_monitoring | mysql radio_monitoring_copy
   ```

2. Upgrade the copy to `022_builtin_roles` with the new code (the report reads a database at 022 only: it refuses an
   earlier revision with exit code 2 and answers `already converted` at 023 or later). From the project root of the
   new version, or in the new image:

   ```bash
   WATCH_WEB_ALEMBIC_URL=mysql+aiomysql://user:password@127.0.0.1:3306/radio_monitoring_copy \
     venv/bin/python -m alembic upgrade 022_builtin_roles
   docker compose run --rm --no-deps -e WATCH_WEB_ALEMBIC_URL=mysql+aiomysql://user:password@127.0.0.1:3306/radio_monitoring_copy \
     api python -m alembic upgrade 022_builtin_roles
   ```

3. Run the report on the copy (read-only: the session refuses writes; `--json` for a file to keep):

   ```bash
   python -m app.backend.cli.authz_report --url mysql+aiomysql://user:password@127.0.0.1:3306/radio_monitoring_copy
   python -m app.backend.cli.authz_report --json --url mysql+aiomysql://user:password@127.0.0.1:3306/radio_monitoring_copy > authz_report.json
   ```

4. Review it. The second line names the administrators after the conversion: the users who will hold the built-in
   `admin` role at the root and are active and not archived (`root_admins` in `--json`), the users the
   last-administrator rule counts. When there are none (for example the legacy administrator role was not the `admin`
   role), a `WARNING` line follows: after the upgrade, archiving or deactivating a user answers 409
   `last_admin_required` and nobody can assign the `admin` role. Recovery after the upgrade: create an administrator
   on the server (a user of type `admin` holding the `admin` role at the root), with the new version (in the image:
   `docker compose run --rm --no-deps api python -m ...`, the certificate in a directory mounted into the container):

   ```bash
   python -m app.backend.cli.init_user /path/to/cert.pem --username <name> --alias "<display name>" --user-type system_administrator
   ```

   Per user: the inferred `type`, the roles, home post and access rows before, the assignments after
   (`role @ root` or `role @ p:<post id>`), `dropped` (permissions that did nothing: the administration permissions of
   a role without `admin.true`, or scoped permissions of a user without any post) and `differences`, what the new
   model cannot express 1:1:
   - `upload_list_follows_manage_any`: the upload list of other users now follows `media.manage_any` at each post;
   - `observation_counters_follow_view`: post counters now need `observation.view` at the post (before `admin.true`
     counted every post);
   - `archived_records_need_archive`: archived posts and users, and archived rows of read-only posts, now need
     `X.archive` where they are;
   - `archived_post_observations_need_post_archive`: archiving observations of an archived post needs `post.archive`
     at the post;
   - `no_permissions`: the user gets no assignment (give them one after the upgrade).

   Then the derived roles: where a role's permissions land on different nodes, the conversion creates
   `<role>_system` (the part at the root), `<role>_posts` (home post and write rows) or `<role>_view` (the `*.view`
   part, for read-only rows), with the part (system, posts, view; in Ukrainian) added to the display name.
5. Deploy as usual (`docker compose build api && docker compose up -d api media-worker`): the start applies 021 -
   023 and seeding creates the built-in role of every type. Every converted user gets an audit row (`action`
   `migrate`, `resource_type` `user`) with the record the report showed; the upgrade of a database that is converted
   already prints `already converted` and changes nothing.
6. Fix the reported differences through the API: the type of a user (`PATCH /users/{user_id}`, `user_type`), the
   assignments (`/users/{user_id}/assignments`), scope groups for the posts (`/scope-groups`, `group_id` of a post).
7. The web client moves to the new auth fields at the same deploy: `is_admin`, `accessible_post_ids`, `roles` and the
   post-access routes are gone (CHANGELOG.md, "Migration guide for the web client").

Downgrade (with the new code, before the old one starts): `python -m alembic downgrade 020_post_archive`. It undoes
what the migrations 021 - 023 and the seeding of the new version changed, and nothing else:

- 023, from the kept tables and its log: `user_roles` and `user_post_access` come back from `user_roles_v1` /
  `user_post_access_v1`, `admin.true`, `post.access_all` and their grants are inserted again with their ids, the
  derived roles, `role_assignments`, `users.user_type` and `authz_v2_migration_log` are dropped.
- 022, from its own log (`builtin_roles_migration_log`): the built-in roles seeding created are deleted with their
  grants (the `admin` role stays: on an upgraded database it existed before, and it gets its texts back), every role
  it renamed to `<key>_custom` gets its old name back (as stored, its case included), the permission texts seeding
  refreshed come back, `media.monitor` is deleted with its grants, then `roles.builtin_key` and the log are dropped.
- 021 deletes `scope_group.*` with their grants and drops `posts.group_id` and `scope_groups`: the groups are lost.

So when nothing was changed through the API, `permissions`, `roles`, `role_permissions`, `users`, `user_roles` and
`user_post_access` come back exactly as they were (same rows, ids and names; checked live on MySQL 8 and MariaDB 11),
and a later upgrade adds no stray roles. A line in the output names a built-in role kept because a `user_roles` row
refers to it, or a role that keeps `<key>_custom` because another role took the old name.

What was done through the API since the upgrade is not undone, it stays as it is:

- changes to roles and grants (permission sets of legacy roles, renames, archives and restores) and to users (home
  post, deactivation, archive, certificates, new users);
- a legacy role deleted permanently (`DELETE /roles/{role_id}`, an archived role) does not come back, and neither do
  its `user_roles` rows: the delete removes its rows of `user_roles_v1` too (the audit row of the delete keeps a full
  copy of them);
- roles created through the API stay, as roles without holders;
- what lived only in the new model is lost with it: assignments, type changes, scope groups. Users created after the
  upgrade have no legacy roles and lose access on a downgrade.

### Upgrading to authorization v3 (tree and system roles, the type as a boundary)

Migrations `029_authz_v3_schema` (`permissions.level`, `roles.kind`, `system_role_assignments`, the conversion log)
and `030_authorization_v3` convert the v2 data (spec `docs/superpowers/specs/2026-10-26-authorization-v3-design.md`,
section 7): the user types `post_admin` and `admin` become `administrator` and `system_administrator`, the built-in
roles `post_admin` and `admin` become `administrator` (every tree permission) and the system role
`system_administrator`, a custom role mixing both levels gives its system permissions to a new `<name>_system` role,
system roles are assigned through `system_role_assignments`, and a custom role named like a new built-in key
(`administrator`, `system_administrator`, `auditor`, `dictionary_editor`, `tag_editor`) becomes `<name>_custom`.
Every user keeps every permission at every node; holders of the former `post_admin` role at a group or the root gain
`scope_group.create/update/archive` there (unit management). The new version refuses to start on a database at 029
without 030 (the permission levels are not written yet).

1. Make a backup first (`BACKUPS.md`).
2. On a copy of the production database at 028 or 029, run the dry run of 030, read-only:

   ```bash
   python -m app.backend.cli.authz_report --v3 --url mysql+aiomysql://user:password@127.0.0.1:3306/radio_monitoring_copy
   python -m app.backend.cli.authz_report --v3 --json --url mysql+aiomysql://user:password@127.0.0.1:3306/radio_monitoring_copy > authz_v3_report.json
   ```

   `unexpected differences` must be 0 (any line there is a user who would lose or gain more than unit management:
   stop and report it); `system administrators after the conversion` names the users who will hold
   `system_administrator`. With none, create one after the upgrade:
   `python -m app.backend.cli.init_user <certificate.pem> --username <name> --alias "<display name>" --user-type system_administrator`.
   `dropped grants` lists grants the conversion does not carry (a system permission below the root, a grant outside
   the boundary of the new type).
3. Deploy; the start migrates, seeds the new built-in roles (`auditor`, `dictionary_editor`, `tag_editor`) and logs
   a warning for every grant that leaves permissions out. Every user and role the conversion changes or creates gets
   an audit row (`action` `migrate`, `resource_type` `user` or `role`) saying what changed. `/health` shows `authorization`
   (`violations`, `system_administrators`).
4. The web client moves to the new contract: `user_type` values, `system_permissions` and `system_assignments` in
   `/auth/me`, `kind` on roles, the system assignment routes (`API.md`).

Rollback: `alembic downgrade 028_no_actor_foreign_keys` (with the new version: the previous one does not know 029)
restores the v2 data exactly from the log and drops the v3 columns and tables, then deploy the previous version.
Users created or retyped after the upgrade get their v2 type back (`administrator` -> `post_admin`,
`system_administrator` -> `admin`, holders of `system_administrator` get the `admin` role at the root); other system
assignments made after the upgrade are lost, and the built-in roles seeding added are deleted.

The database is changed only by the API, migrations, seeding and the restore tool: never fix authorization rows with
hand-written SQL on a server (the start report and `/health` show what to fix through the API).

### Upgrading to PKI client certificates

The application issues the client certificates from now on, from a client CA of its own that only nginx trusts;
the server gets a public certificate (Let's Encrypt). Every certificate made by the scripts from the existing root
keeps logging in until it expires or is revoked. The steps, each explained in `PKI.md`:

1. Server certificate (PKI.md, section 2): a name under your domain in the internal DNS, certbot with DNS-01, nginx
   `ssl_certificate` / `ssl_certificate_key` switched, `nginx -t`, reload. Independent of the application; can go
   first.
2. Deploy the new version as usual. Migration `024_user_certificates` turns every user's certificate into a `legacy`
   row of `user_certificates` with the same hashes: everyone keeps logging in. The web client moves at the same
   deploy (CHANGELOG.md, "PKI client certificates": `PATCH /users/{user_id}` no longer takes a certificate).
3. `PKI_DIR` and the signer (PKI.md, section 3): create the folder with its owners and modes outside the project
   directory, `signer init`, `sign-crl` for the existing root with its key (PKI.md, section 4), then move that key
   offline; fill `.env` (`COMPOSE_FILE`, `PKI_DIR`, `PKI_SIGNER_UID`, `PKI_SOCKET_GID`,
   `MYSETTINGS_PKI__EXTRA_CRL_PATHS`) and
   `signer.env`; `docker compose up -d pki-signer`, `docker compose up -d api`: the first `PKI_DIR/crl/nginx.crl`
   appears (`docker compose exec api python -m app.backend.cli.pki status`).
4. nginx (PKI.md, section 6): `ssl_client_certificate` the bundle, `ssl_crl` the published file,
   `ssl_verify_depth 1`; `nginx -t`; reload.
5. systemd: install and enable `watch-web-crl.path` and `watch-web-crl-refresh.timer` (PKI.md, section 6); run the
   revocation test of PKI.md, section 7.
6. Remove the existing root from every client (`deploy/pki/remove-old-root.cmd`, PKI.md, section 4): login does not
   need it, and no client trusts it for websites any more.
7. Issue new certificates in the web client. Old ones keep working until they expire or are revoked; until then sign
   the existing root's CRL again before its `next_update` (`/health` warns). Then the existing root leaves the bundle
   and `MYSETTINGS_PKI__EXTRA_CRL_PATHS` (PKI.md, section 4, step 5).

Downgrade (with the new code, before the old one starts): `python -m alembic downgrade 023_authorization_v2`. The
users get their certificate columns back: the values of their `legacy` row exactly unless it was revoked (checked
live on MySQL 8 and MariaDB 11: the `users` data come back exactly), otherwise the hashes of their newest active certificate, otherwise a
placeholder that matches no certificate (`unassigned:<user_id>`). The schema differs from the old one only in form:
the unique indexes of the serial and fingerprint hashes come back as `uq_users_pki_certificate_serial_hash` and
`uq_users_pki_certificate_fingerprint_hash` (a database made by the old code names them after the column), and the
four columns come back at the end of the table; uniqueness, types and defaults are the same, and a later upgrade
handles either name. Certificates issued or uploaded since the upgrade, revocations and exports
are lost; the output counts them and names every user who got a placeholder. The old code knows no revocation, so a
`legacy` certificate revoked since the upgrade is not restored: its user gets the hashes of their newest active
certificate, otherwise the placeholder (the output names those users). Turn the PKI override off (`COMPOSE_FILE`)
and put the old `ssl_client_certificate` / `ssl_crl` back in nginx.

### Audit log (migration 031)

Migration `031_audit_events` adds `event`, `category`, `request_id`, `scope_key`, `scope_post_id`,
`scope_group_id` and `params` to `audit_logs`, six indexes, and backfills the existing rows (CHANGELOG, "Audit
log"). The start runs it.

1. Make a backup first (`BACKUPS.md`).
2. Stop the api (and anything else that writes, such as a running `restore_reconcile` or `init_user`) and let the
   start of the new version run the migration: `docker compose up -d` starts it, the api serves only after it.
   The backfill commits every statement on its own, so a writer that is still running is not blocked (measured:
   longest insert 0.07 s), but the rows it writes during the run keep no event (served as `legacy.unknown`) and its
   changes to posts, observations or assignments could make a backfill statement fail on MariaDB (error 1020). A run
   that stops halfway is finished by the next start (only rows without an event are filled).
3. Expected duration: about 3.5 minutes per million audit rows (MariaDB 11.8 in Docker on a workstation, 1 GB buffer
   pool, 1,000,000 rows / 462 MB: 201 s in all). Adding the columns is instant (no table copy, the JSON column
   included), each index takes under a second per million rows, the rest is the backfill (UPDATEs in batches of
   10,000 ids plus one UPDATE per old `legacy.unknown`, role assignment, restore and reconcile row). Count the rows
   first: `SELECT COUNT(*) FROM audit_logs`. While one worker migrates, the other workers wait for its schema lock
   as long as it holds it, logging `Schema lock watch_web_schema_init is held by connection N (migrating)` every 30
   s (a holder that stops frees the lock). The api health check allows a start of up to 20 minutes
   (`start_period` in `docker-compose.yml`), about 5 million audit rows; with more, run the migration on its own
   first, with the api stopped: `docker compose run --rm --no-deps api python -m
   app.backend.cli.migrations_manager migrate`, then `docker compose up -d` (the start finds nothing to migrate and
   seeds).
4. The web must ship a message for every event code (`GET /api/v1/audit-logs/events`) plus `legacy.unknown` before
   it shows the audit page: every row written before the deploy is served as `legacy.unknown` except the restores
   and reconciles, whose counts the backfill moved to `params`.

Rollback: `alembic downgrade 030_authorization_v3`, then deploy the previous version. The downgrade drops the seven
columns and the indexes, and with them the event, params, scope and request id of every row written since the
upgrade (those rows keep only `action`, `resource_type` and their values; `details` is empty on them). The
permission `audit.view_scope` is not owned by a migration (seeding inserts it): its `permissions` row and its grants
stay after the downgrade, and the previous version answers 500 on the permission and role lists that meet it (an
unknown permission name; seeding never deletes it). A rollback that must keep those lists working is the restore of
the backup of step 1 (`BACKUPS.md`), which loses everything written since; otherwise stay on the new version and fix
forward.

### Scheduled cleanup (cron)

Every installation that accepts offloaded uploads needs this job; without it rejected upload bodies (up to the format
limit each, 10 GB by default) pile up in `NGINX_UPLOAD_TEMP` until the disk is full. Add a cron entry on the host, for
example `/etc/cron.d/watch-web` (run as root or as a user in the `docker` group; `/srv/watch_web` stands for the
directory that holds `docker-compose.yml`):

```cron
# minute hour day month weekday user command
15 * * * * root cd /srv/watch_web && docker compose exec -T api python -m app.backend.cli.media_tools sweep-nginx-temp >> /var/log/watch-web-sweep.log 2>&1
```

- `-T`: cron has no terminal, `docker compose exec` must not ask for one.
- Once an hour is enough: the command removes only files older than `storage.nginx_upload_temp_max_age_hours` (24 by
  default), so a younger file nginx is still writing is never touched. Set `--max-age-hours N` only above the longest
  upload you expect.
- Each run prints `removed N file(s), X MB` and, when it could not delete something, `skipped N file(s) ...`; check the
  log after the first runs (`tail /var/log/watch-web-sweep.log`). A skipped file is a permission problem: the API user
  (`MEDIA_UID`) must be able to delete files nginx wrote there (see "Host nginx": the simplest arrangement is
  `MEDIA_UID` = the nginx worker user; `PKI_DIR/issued/` then stays out of its reach, PKI.md section 3).
- The api container must be running; the command uses the same settings (`.env`) as the API.

## Storyboards (seek previews)

The media worker also builds a storyboard (sprite sheets, one ffmpeg pass over the keyframes with the last frame
repeated up to the end; a second, full-decode pass only when that writes nothing, within the same time limit) for
every video of at least `storage.storyboard.min_duration_seconds`, after its poster is ready. It is the heaviest job
the worker does (a long video can take minutes of one CPU core and reads the whole file), so it is limited:

- Upgrade note: storyboards are on by default and need `poster.concurrency` of at least 2. A worker configured with
  `MYSETTINGS_STORAGE__POSTER__CONCURRENCY=1` refuses to start after this upgrade: raise it to `2`, or set
  `MYSETTINGS_STORAGE__STORYBOARD__ENABLED=false`.
- `storyboard.slots` (default 1) storyboards run at once per worker process (several worker containers run N x
  `slots`), and the value must stay below `poster.concurrency`: the worker refuses to start otherwise. A poster is
  always claimed first, a storyboard only when no poster was claimed, so a new upload gets its poster as fast as
  without storyboards. Among storyboards the newest video goes first, so a backfill of old videos does not hold a
  new upload back.
- ffmpeg runs with `storyboard.threads` (default 1) threads and, on POSIX, `storyboard.nice` (default 10); it is
  killed after `storyboard.timeout_seconds` (default 1800). Failures retry after `backoff_seconds`, and the
  storyboard is `failed` after `max_attempts`.
- Stopping the worker (`docker compose stop media-worker`) cancels a running storyboard and gives the attempt back; a
  running poster is allowed to finish.
- At start the worker reconciles: videos without a row get one, ready rows whose files are missing are queued again,
  and `.tmp-` / `.old-` directories under `_derived/storyboards/` older than `timeout_seconds` are removed. A
  filesystem error there (e.g. a wrong owner of `_derived/storyboards/`) is logged and the worker goes on: posters
  never depend on storyboards. Every `upload.sweep_interval_seconds` the worker also queues videos that became
  eligible while it runs (e.g. a duration filled in by `media_tools probe-missing`).
- `storyboard.enabled = false` stops claiming and the API answers `unsupported`; existing files stay.
- Disk: a 10 x 10 sheet of 160 px tiles is roughly 100-200 KB; a two-hour recording (3 sheets) takes about 0.5 MB.
- The sheets are served like thumbnails: signed URLs under `/media-images/` (imgproxy, nginx cache); no nginx change.

```bash
docker compose exec media-worker python -m app.backend.cli.media_tools storyboard-status
docker compose exec media-worker python -m app.backend.cli.media_tools reconcile-storyboards                 # queue missing, repair missing files
docker compose exec media-worker python -m app.backend.cli.media_tools reconcile-storyboards --retry-failed  # also re-queue failed ones
```

`storyboard-status` prints the counts per state (pending, processing, ready, failed) and the latest errors (media id, state, attempts, message). The worker heartbeat
details include `storyboards=<n>` running.

## End-to-end test stack

`tests/docker/compose.test.yml` runs the whole pipeline on a developer machine, including Docker Desktop on Windows: MySQL and nginx are containers there too (a bridge network, not the production layout), but the media path, the real locations and proxy headers (`nginx-app.conf`, `nginx-proxy.conf`) and the uid handling are the same as in production. Only the server block differs (`tests/docker/nginx-server.test.conf`: plain http, no client certificates, the API in development mode).

```bash
docker build -t watch-web .
docker compose -f tests/docker/compose.test.yml up -d
venv/Scripts/python tests/docker/e2e.py        # login, uploads, cache MISS/HIT, signed URLs, worker, Range
venv/Scripts/python tests/docker/formats.py    # every accepted image and video format, rotation, odd sizes, broken files
docker compose -f tests/docker/compose.test.yml down -v
```

The script exits non-zero when any check fails. Ports default to 18089 (nginx) and 18090 (API without nginx); Windows reserves large port ranges below 11000, override with `E2E_NGINX_PORT` / `E2E_DIRECT_PORT` if needed.

## Verification

```bash
# every request presents a user certificate (nginx refuses it otherwise)
CERT="--cert user.crt --key user.key"

# health through nginx
curl -s $CERT https://host/health | head -c 200

# originals: nginx must serve the body, the API only the redirect
curl -sI $CERT -H "Authorization: Bearer $TOKEN" https://host/api/v1/media/1/download | grep -i "x-served-by\|accept-ranges"

# thumbnails: first call MISS, second HIT
curl -sI $CERT -H "Authorization: Bearer $TOKEN" "https://host/api/v1/media/1/thumbnail?size=medium" | grep -i "x-cache\|content-type"

# public URL from a list response loads without a token (the certificate is still required)
curl -sI $CERT "https://host/media-images/<signature>/rs:fit:300:300:0:0/q:85/exp:.../<source>" | head -1

# queue and worker
docker compose exec media-worker python -m app.backend.cli.media_tools poster-status
docker compose exec media-worker python -m app.backend.cli.media_tools storyboard-status
```

`X-Cache: HIT` means nginx served from cache; `MISS` means imgproxy rendered. A 403 from `/media-images/` is an invalid or expired signature; a 422 from imgproxy usually means the source is missing under `MEDIA_ROOT` or exceeds `IMGPROXY_MAX_SRC_RESOLUTION`. Images above that limit get `thumbnail_status: unsupported` from the API (it reads the size from the header at upload), as long as the API has the same limit: docker-compose.yml passes `IMGPROXY_MAX_SRC_RESOLUTION` from `.env` (default 250 megapixels, enough for 200 MP phone cameras) to both containers. The limit bounds CPU time, not memory: libvips decodes a JPEG at a reduced scale and a PNG line by line, so a 200 MP source peaked at 63 MB (JPEG) and 124 MB (PNG) per render, and four 1600 px previews of a 200 MP PNG at once at 431 MB, under the 1 GB container limit. What it stops is a crafted file: a 274 KB PNG holding 1.6 gigapixels took 3.5 s of CPU and 305 MB per render, and the per-format file size limit cannot catch it.

## Troubleshooting

| Symptom | Cause |
|---------|-------|
| Thumbnail returns 502 through nginx | imgproxy container down, or `upstream imgproxy` points to the wrong port |
| Thumbnail returns 403 through nginx | key/salt mismatch between `.env` (`MYSETTINGS_STORAGE__IMGPROXY__*`) and the imgproxy container |
| imgproxy logs "permission denied" or 422 | `MEDIA_ROOT` not readable by `MEDIA_UID`, or mounted at a different path than `IMGPROXY_LOCAL_FILESYSTEM_ROOT` |
| Every request is `X-Cache: MISS` | cache directory not writable by nginx, or `nginx-http.conf` not loaded in the http context |
| Videos stay `pending` | media-worker container not running, or it cannot connect to MySQL on 127.0.0.1 |
| Uploads through nginx fail with "temp file not found" | `NGINX_UPLOAD_TEMP` differs from `client_body_temp_path`, or the directory is not mounted into the api container |
| API cannot connect to MySQL | MySQL not listening on TCP 127.0.0.1, or grants only for the socket user |
| Files written by containers unreadable by nginx | `MEDIA_UID`/`MEDIA_GID` do not match the owner of `MEDIA_ROOT`, or the share was mounted with restrictive `file_mode` |
| The API stops at start with "unit structure is broken" | A corps sits under a group or a brigade sits outside a corps (no API route creates one; hand-edited or old data). The message names the ids. Move a brigade into a corps: `UPDATE scope_groups SET parent_id = <corps id>, parent_key = <corps id> WHERE id = <brigade id>;` (a corps goes back under the root: `parent_id = NULL, parent_key = 0`), then start again |
| nginx 400 for a client certificate, browser certificate errors, 503 on issuing a certificate | see `PKI.md`, section 9 |

## Without Docker

The same image contents run from a venv: `python -m uvicorn app.main:fastapi_app --host 127.0.0.1 --port 8000 --workers 4 --proxy-headers` for the API, `deploy/systemd/watch-web-media-worker.service` for the worker, and imgproxy as a Podman or Docker container (it is distributed as an image only). Settings come from `.env`; `MYSETTINGS_STORAGE__UPLOAD_PATH` must equal `MEDIA_ROOT`. The scheduled cleanup runs from the venv instead of the container: `15 * * * * watch-web cd /srv/watch_web && venv/bin/python -m app.backend.cli.media_tools sweep-nginx-temp >> /var/log/watch-web-sweep.log 2>&1` (as the user the API runs as, from the project root so `.env` is read).
