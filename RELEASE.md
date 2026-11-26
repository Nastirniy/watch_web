# Releases: build, deploy, verify, roll back

This is the one runbook for putting a new version of watch-web on a server and taking it back. Every step is a
plain command; nothing here needs anything but `docker`, `docker compose`, `sh` and the project's own commands.
Database backups in depth: BACKUPS.md. Server setup (nginx, MariaDB, PKI): DEPLOYMENT.md and PKI.md. The web team's
side of a release (local testing against a release, publishing the web client): WEB_DEV_AND_RELEASE.md.

## What a release is

- **One image per commit**, used by every watch-web service: `api`, `media-worker`, `pki-signer`, the tools
  `db-backup` and `migrate`. Its tag is `<YYYY.MM.DD>-<sha7>`: the commit's date (UTC) and the first 7 characters of
  its hash, for example `watch-web:2026.11.24-2a8c1e7`. Building the same commit again gives the same tag.
- **`.env` names the release** with one line, `WATCH_WEB_TAG=2026.11.24-2a8c1e7`. `deploy/release.sh` changes that line
  and nothing else in `.env`. `docker compose` never builds or downloads a watch-web image (`pull_policy: never`):
  an image is built with `release.sh build` and, on a server without internet, brought with `release.sh load`.
- **imgproxy is pinned** in `.env` (`IMGPROXY_IMAGE`, `IMGPROXY_IMAGE_ID`), see "Pin imgproxy".
- **The image knows its version**: `/health` shows `version` (the tag) and `revision` (the commit); so do
  `docker image inspect` (OCI labels) and `release.sh status`.
- **A rollback by one release never needs a database restore**: the previous release always runs on the new schema
  (it neither migrates nor seeds then). A release that breaks this on purpose (a "contract" migration) makes older
  code refuse to start with one line, and `release.sh rollback` says so before it changes anything. See "Release
  rules for developers".

## Quick reference

Run every command from the project directory on the server (the folder with `docker-compose.yml` and `.env`), as a
user allowed to use docker (`sudo` on most servers: `sudo deploy/release.sh status`).

```
deploy/release.sh build                    # build machine: build watch-web:<tag> from the checked-out commit
deploy/release.sh save <tag> <file>        # build machine: image + imgproxy into <file> and <file>.sha256
deploy/release.sh load <file>              # server: check the checksum, docker load
deploy/release.sh status                   # server: release in .env, services, schema, history
deploy/release.sh deploy <tag>             # server: backup, migrate, switch, restart, wait
deploy/release.sh rollback                 # server: back to the release before the current one
deploy/release.sh prune --keep 3           # server: remove old watch-web images
```

Options: `--dry-run` (every check and the plan, nothing changed), `--yes` (no question; for scripts),
`--no-backup` (deploy where the backup service is not set up; take a backup yourself first), `--rebuild` (build an
existing tag again), `--keep N` (prune).

Every command that changes something (deploy, rollback, prune) first runs all its checks, prints a numbered plan and
asks `Proceed? [y/N]`. Anything but `y` or `yes` stops it with nothing changed.

## Preconditions

On the server, before the first deploy (check each):

- [ ] `docker version` and `docker compose version` answer (compose v2).
- [ ] The project directory holds the deployment files of the release you deploy (see "Move a release to the
      server"); `deploy/release.sh` is executable (`chmod +x deploy/release.sh`).
- [ ] `.env` has `WATCH_WEB_TAG=<the running release>`, `IMGPROXY_IMAGE=` and `IMGPROXY_IMAGE_ID=` (see "Pin
      imgproxy"). First time on a running server: "Adopt release.sh on a running server".
- [ ] Backups are set up: `COMPOSE_FILE` in `.env` includes `docker-compose.backup.yml` (BACKUPS.md, Setup).
      `docker compose run --rm db-backup status` answers. Without them `deploy` refuses unless `--no-backup`.
- [ ] `docker compose config -q` prints nothing (it names any variable missing from `.env`).
- [ ] Free disk: the backup needs about the size of a compressed dump (`docker compose run --rm db-backup list`
      shows the last ones), the image about 1.2 GB (`df -h / /var/lib/docker <BACKUP_DIR>`). `release.sh` refuses
      below 2 GB free in `BACKUP_DIR` (`RELEASE_MIN_FREE_MB` changes it).

On the build machine: a git checkout of the source, docker, internet (the Python base image and the Python
packages are downloaded during the build).

## Pin imgproxy

imgproxy is not built here; it is the official image at an exact version. `.env` holds it in two lines:

```
IMGPROXY_IMAGE=ghcr.io/imgproxy/imgproxy:v3.31.4
IMGPROXY_IMAGE_ID=sha256:<the image id>
```

Read both on the server after the image is there (pulled or loaded); never type them from memory:

```
docker image inspect --format '{{index .RepoDigests 0}} {{.Id}}' ghcr.io/imgproxy/imgproxy:v3.31.4
```

The second value is the image id: put it into `IMGPROXY_IMAGE_ID`. It is a digest of the image's content and stays
the same after `docker save` / `docker load`. `release.sh deploy` refuses when the image in `IMGPROXY_IMAGE` has
another id, so a different image under the same tag never runs unnoticed. `IMGPROXY_IMAGE` may also carry the digest
(`ghcr.io/imgproxy/imgproxy:v3.31.4@sha256:<digest>`, the first value above) where that reference still resolves
after `docker load` (check: `docker image inspect '<IMGPROXY_IMAGE>'` answers); the plain version tag plus the id
check is enough.

To move to a newer imgproxy: pull it on a machine with internet, test it, carry it over with `release.sh save` /
`load`, change both lines in `.env`, then `docker compose up -d imgproxy`.

## Adopt release.sh on a running server

Once, on a server that runs a build from before release.sh (its image is `watch-web` or `watch-web:latest`, `.env`
has no `WATCH_WEB_TAG`). Nothing restarts.

1. Note the commit the server runs (from your deploy notes) and give its image a release tag:
   `docker tag watch-web:latest watch-web:<YYYY.MM.DD>-<sha7>` (the commit's date and hash; on the build machine:
   `TZ=UTC git show -s --date=format-local:%Y.%m.%d --format=%cd <commit>` and `git rev-parse --short=7 <commit>`).
2. Unpack the deployment files of the first release with release.sh into the project directory ("Move a release
   to the server", step 3). The running containers are not touched.
3. Add to `.env`: `WATCH_WEB_TAG=<the tag of step 1>`, `IMGPROXY_IMAGE=` and `IMGPROXY_IMAGE_ID=` of the imgproxy that
   runs (`docker compose ps imgproxy` shows its image; "Pin imgproxy").
4. `docker compose config -q` prints nothing; `deploy/release.sh status` shows the tag and the services.

`release.sh rollback` from the first release goes back to the tag of step 1: that image predates the schema check,
so release.sh compares its migration head with the database revision instead (the first release with release.sh
has no migration, so they match).

## Build

On the build machine, in the source checkout, at the commit to release (no uncommitted changes):

```
git checkout <commit or branch>
deploy/release.sh build
```

Output ends with `Built watch-web:<tag>`. Keep the tag: every later step names it. `build` refuses a working tree
with uncommitted changes (a tag must name exactly one commit) and skips a tag that exists already (`--rebuild`
builds it again). Check the image: `docker image inspect watch-web:<tag> --format '{{json .Config.Labels}}'` shows
version, revision and created.

## Move a release to the server

A release is the image plus the deployment files of the same commit (compose files, `deploy/`, nginx snippets,
`.env.example`, this file).

1. On the build machine, pull the pinned imgproxy once (`docker pull ghcr.io/imgproxy/imgproxy:v3.31.4`), then:
   ```
   IMGPROXY_IMAGE=ghcr.io/imgproxy/imgproxy:v3.31.4 deploy/release.sh save <tag> watch-web-<tag>.tgz
   git archive --format=tar.gz -o watch-web-<tag>-files.tar.gz HEAD
   ```
   `save` writes `watch-web-<tag>.tgz` and `watch-web-<tag>.tgz.sha256`. A server with internet and the source can
   instead build there (`deploy/release.sh build` in its checkout) and skip `save` / `load`.
2. Copy the three files to the server (scp, a USB disk), next to each other.
3. On the server:
   ```
   cd /path/to/watch_web
   tar -xzf /path/to/watch-web-<tag>-files.tar.gz        # updates the tracked files; .env, signer.env and
                                                         # deploy/release-history.log are not in it
   deploy/release.sh load /path/to/watch-web-<tag>.tgz
   ```
   `load` checks the `.sha256` first and refuses a damaged archive. It prints `Loaded image: watch-web:<tag>` and
   the imgproxy line. Unpacking the files does not restart anything; the running release keeps running.

The new compose files run the old release too (a rollback keeps them).

## Deploy

```
deploy/release.sh deploy <tag> --dry-run      # checks and plan only
deploy/release.sh deploy <tag>
```

Example:

```
Checks:
  ok   docker 27.3.1, docker compose 2.29.7
  ok   docker compose config
  ok   image watch-web:2026.11.24-2a8c1e7 (revision 2a8c1e7..., created 2026-11-24T09:12:00Z)
  ok   schema: Schema check: database at 037_outbox_events; release 2026.11.24-2a8c1e7 migrates it to 038_x
  ok   imgproxy image id matches IMGPROXY_IMAGE_ID
  ok   .env writable
  ok   backup service configured, 41000 MB free in /srv/watch_web/backups
Plan (running release 2026.11.20-73a93fa):
  1. back up the database (label pre-upgrade, kept 90 days), a few minutes
  2. migrate the database with the new release while the old one keeps serving
  3. set WATCH_WEB_TAG=2026.11.24-2a8c1e7 in .env (old file kept as .env.bak)
  4. restart the services on the new release (about 30 s without service)
  5. wait until every service is healthy (up to 1200 s while the api finishes its start)
Proceed? [y/N]
```

What each step does:

1. **Backup**: `docker compose run --rm -T db-backup create --label pre-upgrade`, with the running release's image.
   It prints the backup id (`20261124T101420Z-pre-upgrade`), which goes into the history line. Kept 90 days.
2. **Migrate**: `WATCH_WEB_TAG=<tag> docker compose run --rm -T migrate`: the new release migrates the database
   under the same lock the api takes at its start. The old release keeps serving meanwhile: it runs on the new
   schema, which is the promise every normal migration keeps.
3. **Switch**: `.env` gets the new `WATCH_WEB_TAG`; the previous file is kept as `.env.bak`. The file is written to
   `.env.tmp` and renamed, so it is never half written.
4. **Restart**: `docker compose up -d` recreates api, media-worker and pki-signer on the new image (graceful stop:
   open requests get 25 s, event streams are told to reconnect).
5. **Wait**: until every service runs, is healthy (api, media-worker, pki-signer and imgproxy have health checks),
   and runs the new image.

A failure in steps 1 to 3 leaves the running release as it was (exit 2): correct the cause and deploy again. A
failure in steps 4 or 5 prints the rollback command (exit 5).

## Verify

```
deploy/release.sh status
curl -s http://127.0.0.1:8000/health/ready    # {"ready": true, "version": "<tag>", "revision": ..., "schema_revision": ...}
curl -s http://127.0.0.1:8000/health          # "version": "<tag>", "status": "healthy", "schema": {..., "compatible": true}
docker compose ps                             # api, media-worker, pki-signer and imgproxy healthy
docker compose logs --since 15m api | grep -E "Schema check|SCHEMA CHECK|Server ready|ERROR"
docker compose logs --since 15m media-worker | grep -E "Schema check|SCHEMA CHECK|ERROR"
```

`/health` and `/health/ready` answer only from the allowed hosts (127.0.0.1 on the server); `/health` at most 10
times a minute per client, `/health/ready` 60. `/health/ready` answers 503 `service_not_ready` with `reason_code`
`database_unreachable` (MariaDB down or unreachable) or `schema_out_of_range` (this release does not run on the
database's schema: the `SCHEMA CHECK FAILED` line is in the api log). The health checks of the containers: the api
asks `/health/ready` every 30 s (an image from before it: `/health`), the media worker is healthy while it writes
its heartbeat (a file it touches, younger than 3 heartbeats), pki-signer while it answers on its socket.
Then open the web client and do one ordinary thing (log in, open a post, upload a small photo).

## Roll back

```
deploy/release.sh rollback --dry-run
deploy/release.sh rollback
```

It takes the release before the current one from `deploy/release-history.log` (the `deploy <old> -> <current> ok`
line), runs that image's schema check against the database, then sets `.env`, restarts and waits. No backup, no
migration: the old release runs on the new schema without migrating or seeding, and logs
`Schema check: database at <new revision>, newer than this release (<its head>); allowed (min code head ...); not
migrating, not seeding`.

When a contract migration came after the old release, the check refuses and nothing changes (exit 3):
`release.sh: watch-web:<old> does not run on the database's schema (a contract migration came after it): a
rollback needs the database restore. Fix: RELEASE.md, Restore the database; the backup of that deploy is <id>.`

Rolling back twice goes one release further back each time. To return to the newer release:
`deploy/release.sh deploy <newer tag>` (it backs up and migrates, which is then nothing to do).

## Restore the database

Only when a rollback is not enough: the rollback refused (contract migration), or the data itself must go back.
The backup taken by the deploy is named in the history line (`backup=...`) and in `release.sh status`.

```
docker compose run --rm db-backup verify <id> --deep
docker compose stop api media-worker
docker compose run --rm db-backup restore <id>          # shows the plan, asks for "yes"
deploy/release.sh deploy <the release that made the backup>
```

then the steps after a restore in BACKUPS.md ("Restore (rollback)": the CRL, `restore_reconcile`). The restore tool
of the running release knows every revision up to its own, so it restores a backup an older release took. A backup
a NEWER release took (after a rollback) needs that release's tool:
`WATCH_WEB_TAG=<newer tag> docker compose run --rm db-backup restore <id>`.

## Prune

```
deploy/release.sh prune --keep 3 --dry-run
deploy/release.sh prune --keep 3
```

Removes `watch-web:*` images except the newest N, the one in `.env`, and every tag named by the last N history lines
(so a rollback target stays). Build cache is separate: `docker builder prune -af`.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | done, or the dry run passed |
| 1 | usage error (unknown command or option, missing argument) |
| 2 | a check or a step before the switch failed; the running release is untouched |
| 3 | the schema check refused (the release does not run on the database's schema) |
| 4 | the operator answered no; nothing changed |
| 5 | switched to the new release, but it was not healthy in time; the line names the rollback |

## What the output lines mean

| Line | Meaning |
|------|---------|
| `ok   docker ..., docker compose ...` | docker and compose v2 answer |
| `ok   docker compose config` | `.env` and the compose files resolve (no missing variable) |
| `ok   image watch-web:<tag> (revision ..., created ...)` | the image is on this machine; its commit and commit time |
| `ok   schema: Schema check: database at R; release T migrates it to H` | deploy: the new release knows the database and migrates it |
| `ok   schema: Schema check: database at H, the head of release T` | nothing to migrate |
| `ok   schema: Schema check: database at R, newer than this release (H); allowed ...` | rollback: the old release runs on the newer schema |
| `ok   schema: watch-web:<tag> predates the schema check; ...` | rollback to the build before release.sh: its head is the database revision |
| `ok   imgproxy image id matches IMGPROXY_IMAGE_ID` | the pinned imgproxy is the one here |
| `ok   backup service configured, N MB free in DIR` | the deploy will back up first |
| `--   no backup service (--no-backup)` | deploy without a backup; you took one yourself |
| `1. Backing up ... / backup <id>` | the pre-upgrade backup and its id |
| `Not ready after N s:` followed by service lines | what was still starting, unhealthy or on the old image |
| `Dry run: every check passed, nothing changed.` | `--dry-run` ends here |
| `Nothing changed.` | you answered no |

In the logs of the services:

| Line | Meaning |
|------|---------|
| `Schema check: database at R; release T migrates it to H` | api start: migrating (long migrations: minutes, see DEPLOYMENT.md) |
| `Schema check: database at R, newer than this release (H); allowed (min code head M); not migrating, not seeding` | an older release runs on a newer schema (after a rollback): normal |
| `SCHEMA CHECK FAILED (api): ...` | see "Failures" below; the container exits 3 and Docker retries with a growing delay |
| `SCHEMA CHECK FAILED (worker): ...` | the worker started before the api migrated; it retries by itself |
| `Built-in role X holds permissions this release does not grant it: [...]` | an older release on a newer schema: the newer release added them; left as they are |

## Failures and what to do

Every error of release.sh is one line on stderr: `release.sh: <what failed>. Fix: <what to do>.`

| Line starts with | Cause | What to do |
|------------------|-------|------------|
| `release.sh: docker does not answer` | docker not running or no permission | `systemctl start docker`; run with `sudo` |
| `release.sh: docker compose v2 is missing` | old docker-compose v1 or no plugin | install the docker compose plugin |
| `release.sh: no docker-compose.yml in` | run from another directory | `cd` to the project directory |
| `release.sh: docker compose config failed` | a variable missing in `.env` (named above the line) | add it (`.env.example` lists them) |
| `release.sh: image watch-web:<tag> is not on this machine` | not built or not loaded here | `release.sh load <file>`; `docker image ls watch-web` |
| `release.sh: the schema check of watch-web:<tag> failed (exit N)` | database unreachable, or a broken image | read the lines above it; check MariaDB and `MYSETTINGS_DATABASE__*` |
| `SCHEMA CHECK FAILED (migrate): ...` (deploy, exit 3) | the database is at a revision this release does not know and admits only newer code | deploy the newer release that migrated it, or restore |
| `release.sh: IMGPROXY_IMAGE is not set in .env` / `IMGPROXY_IMAGE_ID is not set` | not pinned yet | "Pin imgproxy" |
| `release.sh: imgproxy image ... is not on this machine` | not loaded | `release.sh load <file>` from a `save` that included it |
| `release.sh: imgproxy image ... has id ..., .env says IMGPROXY_IMAGE_ID=...` | another image under that tag | bring the pinned one; or, after testing the new one, update `IMGPROXY_IMAGE_ID` |
| `release.sh: no .env in` / `.env or its directory is not writable` | wrong directory or user | run as the owner of the project directory (`sudo`) |
| `release.sh: only N MB free in DIR, the backup needs at least M MB` | disk nearly full | `docker builder prune -af`, `release.sh prune`, `db-backup prune` |
| `release.sh: the backup service db-backup is not configured` | `docker-compose.backup.yml` not in `COMPOSE_FILE` | set up backups (BACKUPS.md) or take one yourself and use `--no-backup` |
| `release.sh: the backup failed, nothing was changed` | the backup tool refused or failed (its code is above) | BACKUPS.md, "Commands" and the refusal table; deploy again |
| `release.sh: the migration failed; the running release keeps serving` | a migration raised (the log above) | keep the old release; send the log to the developers; restore only if the old release cannot run on the partly migrated schema (`release.sh status`) |
| `release.sh: docker compose up failed on the new release` (exit 5) | a container could not be created | `deploy/release.sh rollback`, then `docker compose logs` |
| `release.sh: the new release is not healthy after N s` (exit 5) | the api or worker does not get healthy | `deploy/release.sh rollback`, then `docker compose logs api media-worker` |
| `release.sh: WATCH_WEB_TAG is not set in .env` | server not adopted yet | "Adopt release.sh on a running server" |
| `release.sh: no deploy/release-history.log` / `no successful deploy of <tag>` | release.sh never deployed the current release | `release.sh deploy <tag>` of the release you want |
| `release.sh: <tag> was deployed over a release release.sh did not know` | the deploy before had no tag in `.env` | tag the old image ("Adopt", step 1), or restore |
| `release.sh: watch-web:<old> does not run on the database's schema ...` (exit 3) | a contract migration after the old release | "Restore the database" with the backup the line names |
| `release.sh: docker compose up failed on <old>` / `watch-web:<old> is not healthy` (rollback, exit 5) | the old release does not start | `docker compose logs`; `release.sh deploy <newer>` goes back |
| `release.sh: the working tree has uncommitted changes` (build) | local edits | commit or stash them |
| `release.sh: <dir> is not a git checkout` (build) | build run outside the source | run build in the checkout |
| `release.sh: docker build failed` | the build output above says why | fix and build again |
| `release.sh: IMGPROXY_IMAGE is not set (environment or .env)` (save) | no imgproxy named | `IMGPROXY_IMAGE=... deploy/release.sh save ...` |
| `release.sh: docker save failed` | imgproxy not pulled, or no room | `docker pull <imgproxy>`; free space |
| `release.sh: no file` / `no <file>.sha256 next to <file>` (load) | files not copied together | copy the archive and its `.sha256` |
| `release.sh: the checksum of <file> does not match` | damaged on the way | copy it again |
| `release.sh: docker load failed` | no room in `/var/lib/docker` | free space, load again |
| `SCHEMA CHECK FAILED (api): the database is at R, which release T (code head H) does not know, and the database admits only code at M or newer.` (api log, container restarting) | an older release started on a contract schema | `deploy/release.sh deploy <newer tag>` or restore the pre-upgrade backup |
| `SCHEMA CHECK FAILED (worker): the database is at R, release T needs H.` | worker started before the api migrated | wait; it retries. If it stays: `docker compose up -d api` |
| `SCHEMA CHECK FAILED (api): the database is unreachable` | MariaDB down or wrong credentials | start MariaDB; check `.env` |

## How long each step takes

Measured on the test server (VM, 4 cores, database about 233 MB as a dump): filled in after the live test.

| Step | Time |
|------|------|
| build (build machine, cached base) | measured in Task 7 |
| save / copy / load (about 1.2 GB) | measured in Task 7 |
| backup (pre-upgrade) | measured in Task 7 |
| migrate (no migration) | measured in Task 7 |
| restart until healthy (no service) | measured in Task 7 |
| rollback until healthy | measured in Task 7 |

## Where things are

- **Backups**: `BACKUP_DIR/<id>/` (`dump.sql.gz` + `manifest.json`), id `<time>-pre-upgrade`, kept 90 days by
  the backup retention (BACKUPS.md, "Layout"). `docker compose run --rm db-backup list` lists them.
- **History**: `deploy/release-history.log` in the project directory (not in git), one line per action:
  `2026-11-24T10:15:03Z deploy 2026.11.20-73a93fa -> 2026.11.24-2a8c1e7 ok backup=20261124T101420Z-pre-upgrade by=admin`;
  failures end with `failed step=backup|migrate|up|wait`. Rollback reads it; keep it.
- **`.env.bak`**: `.env` before the last deploy or rollback.

## Release rules for developers

- **Expand by default.** A migration adds (tables, nullable columns, indexes, new enum values the old code never
  reads) and declares `CONTRACT = False`. The release before it keeps running on the new schema; that is what makes
  `release.sh rollback` safe.
- **Contract later.** Removing or narrowing (drop a column, table or index, rename, `NOT NULL`, a type change)
  declares `CONTRACT = True` and calls `set_min_code_head(op, "<revision>")` in `upgrade()`
  (`app/backend/migrations/support/compat.py`). Name the previous release's head when that release no longer reads
  what you remove (ship the code that stops reading it one release earlier): the rollback still works. Name the
  migration's own revision when older code would break: older code then refuses to start, and a rollback needs the
  database restore. `downgrade()` puts back the value before (or `None`).
  `tests/migrations/test_migration_policy.py` refuses a narrowing statement without the marker.
- **What the test cannot see, mark by hand** (`CONTRACT = True`): a stored value older code cannot read (a new value
  of a status or reason column the old enum does not have: migration 034 is the example), and a change of a
  protocol shared through the database (job claims, the move claim and its lock order, status values another
  process acts on).
- **No migration in a release that must roll back to a build from before the schema check** (the first release with
  release.sh).
- **Base image**: `ARG PYTHON_BASE_IMAGE` in the Dockerfile pins `python:3.13-slim` by digest. To update:
  `docker pull python:3.13-slim`, `docker image inspect python:3.13-slim --format '{{index .RepoDigests 0}}'`, put the
  digest there, commit, build, test.
- **One image, one tag**: never `docker build -t watch-web:<tag>` by hand for a server, never edit a running
  container; `release.sh build` is the only way a release tag is made.
