# Database backups

How the database of watch_web is backed up, checked and rolled back. Design:
`docs/superpowers/specs/2026-10-22-db-backups-design.md` (local). Settings: `ENV.md`, "Database backups".

## What it is

- **The tool:** `mariadb-dump --single-transaction`, the standard logical dump. It takes a consistent snapshot while
  the API keeps running (all tables are InnoDB) and works the same on MariaDB 11.6+ and MySQL 8 (`mysqldump`).
- **The wrapper:** our command around it, `python -m app.backend.cli.db_backup`. It keeps a catalog of the backups,
  applies retention, checks backups, copies them off the host, and restores safely, with undo.
- **Where it runs:** the one-off compose service `db-backup` (the `watch-web` image, which carries `mariadb-client`
  11.8). A systemd timer runs it daily.
- **What the API does:** it only reads. `GET /api/v1/database/status` (`database.view`) and
  `GET /api/v1/database/backups` (`backup.view`) report, and `/health` has a `backups` part. Nothing in the API starts
  a backup or a restore.
- **What it covers:** the database only. Media files (`MEDIA_ROOT`) and the PKI directory need their own backup (see
  "What a rollback does not bring back").

Recovery point: at most a day with the daily timer (6 hours with the 6-hour timer below). For a point in time
(minutes), see "Bigger databases, point in time".

## Layout

```
BACKUP_DIR/
  20261022T031500Z-scheduled/   dump.sql.gz (gzip of the dump) + manifest.json (written last; id, label, times,
                                server and tool version, migration revision, tables, size, sha256)
  .tmp-<id>/                    a run in progress (removed by the next run once older than the timeout)
  .state/last-run.json          outcome of the last run: ok / failed + error code
  .state/lock                   one writer at a time (a second command answers `refused: busy`)
```

A folder is a backup only with a valid `manifest.json`. The catalog is these files, never the database, so a
restore cannot erase the list of backups. Labels:

| Label | Made by | Kept |
|-------|---------|------|
| `scheduled` | the timer | the newest per day for 7 days, per ISO week for 4 weeks, per month for 6 months |
| `manual` | an operator | 90 days |
| `pre-upgrade` | the upgrade steps in DEPLOYMENT.md | 90 days |

The newest backup is never deleted, whatever its age. Retention runs after every `create` and on `prune`.

## Setup

1. Create the directory on the host. It must be outside the project directory (the build context), mode 0700, and
   owned by `MEDIA_UID`. Backups hold password and token hashes: the nginx user, which may share `MEDIA_GID`, must
   never read them.

   ```bash
   sudo install -d -m 0700 -o "$MEDIA_UID" -g "$MEDIA_GID" /srv/watch_web/backups
   ```

2. Grant the application database user what the tool needs, besides its own database:
   - the staging, verify and kept schemas (`radio_monitoring_restore`, `_verify`, `_before_*`);
   - `PROCESS`, so the restore sees other connections. The api and the worker connect as the same user, so they
     are counted even without it.

   ```sql
   GRANT ALL ON `radio\_monitoring\_%`.* TO 'radio_user'@'127.0.0.1';
   GRANT PROCESS ON *.* TO 'radio_user'@'127.0.0.1';
   ```

3. `.env`:

   ```bash
   COMPOSE_FILE=docker-compose.yml:docker-compose.backup.yml          # + :docker-compose.pki.yml with PKI
   BACKUP_DIR=/srv/watch_web/backups
   ```

4. Rebuild and restart the api: `docker compose build api && docker compose up -d api`. The image now has the
   client, and the api mounts `BACKUP_DIR` read-only. The api refuses to start if the directory cannot be read.
5. First backup and check:

   ```bash
   docker compose run --rm db-backup create --label manual
   docker compose run --rm db-backup verify latest --deep
   ```

6. The timer (edit `WorkingDirectory` in the service first):

   ```bash
   sudo cp deploy/systemd/watch-web-db-backup.service deploy/systemd/watch-web-db-backup.timer /etc/systemd/system/
   sudo systemctl daemon-reload && sudo systemctl enable --now watch-web-db-backup.timer
   systemctl list-timers watch-web-db-backup.timer
   ```

   Every 6 hours instead of daily: set `OnCalendar=*-*-* 00/6:15:00` in the timer and
   `MYSETTINGS_BACKUP__STALE_AFTER_HOURS=8` in `.env`.

### Off-host copy

A backup on the same disk is lost with the disk. In production, mount a network share (NFS or SMB, ideally
encrypted and on another machine) on the host before docker starts, then:

```bash
COMPOSE_FILE=docker-compose.yml:docker-compose.backup.yml:docker-compose.backup-copy.yml
BACKUP_COPY_DIR=/mnt/backup-share/watch_web
```

How the copy works:
- Every `create` copies the new backup there through `.tmp-<id>/`, checks the checksum from the copy, then renames
  it. A failed copy keeps the local backup, records `copy_failed` and exits 1.
- Retention runs on the share too, on its own.
- `GET /database/backups` shows `copied` per backup, and `/health` is degraded while the newest backup has no copy.

## Commands

All of them run as `docker compose run --rm db-backup <command>`.

| Command | Does |
|---------|------|
| `create [--label manual\|pre-upgrade] [--no-copy]` | a backup; prints its id |
| `list [--json]` | backups newest first: id, size, age, revision, copy |
| `verify <id\|latest> [--deep]` | checksum and gzip. `--deep` also loads the backup into `<db>_verify`, compares its tables and revision with the manifest, then drops it |
| `restore <id\|latest> [--yes] [--force-connections] [--force-orphans]` | roll the database back (below) |
| `undo-restore [<schema>] [--yes] ...` | swap the newest (or the named) kept schema back in |
| `prune [--dry-run]` | retention, plus kept schemas older than `keep_previous_days` (7) |
| `status [--json]` | what the API reports |

Exit codes:

| Code | Meaning |
|------|---------|
| 0 | ok |
| 1 | failed (`failed: <code>`) |
| 2 | not configured or bad usage |
| 3 | refused, nothing was changed (`refused: <code>`) |

Error codes:

| Code | Cause |
|------|-------|
| `no_space` | free space below 1.5 x the last backup + `min_free_mb` |
| `busy` | another command holds the lock |
| `unsupported_objects` | the database holds a view, trigger, routine, event or non-InnoDB table; the names are in the log |
| `dump_failed` | the dump tool failed; its message is in the log |
| `timeout` | the dump or load ran past `dump_timeout_seconds` |
| `copy_failed` | the copy to the share failed or did not match its checksum |
| `tools_missing` | the client tools are not in the image |
| `load_failed` | the loaded backup did not match its manifest |

The restore and undo refusals are listed under "Restore (rollback)" below.

Daily check: `status`, or `GET /api/v1/database/backups`, or `/health` on the host (`backups.status`).

## Restore (rollback)

Runbook:

```bash
docker compose run --rm db-backup list
docker compose run --rm db-backup verify <id> --deep
docker compose stop api media-worker
docker compose run --rm db-backup restore <id>           # shows the plan, asks for "yes"
docker compose up -d
docker compose exec api python -m app.backend.cli.pki crl   # with PKI: publish the CRL now
```

What `restore` does:

1. It checks the checksum, and that this version of the code knows the backup's migration revision. A backup newer
   than the code is refused (`unknown_revision`); an older one is fine, because the api migrates forward when it
   starts.
2. It refuses while other connections use the database (`connections`). Stop api and media-worker first.
   `--force-connections` exists for a stuck connection you have checked.
3. It loads the backup into `<db>_restore` and checks its tables and revision against the manifest. The live
   database is untouched until step 5. Any failure drops the staging schema.
4. Carry-over: the restored data keeps what the project treats as final.
   - **Revocations:** a certificate revoked in the live data stays revoked.
   - **Certificates issued after the backup:** they come back as revoked (`cessation_of_operation`), so the next
     CRL still lists them.
   - **CRL number:** it never goes backwards.
   - **Sessions:** every session of the backup is ended (`database_restored`), so users log in again.
   - **Orphans:** a post-backup certificate whose user is missing from the backup makes the restore refuse
     (`orphan_certificates`, serials listed). `--force-orphans` skips those certificates, and the tool prints their
     serials so you can revoke them by hand.
5. Swap: one atomic `RENAME TABLE` moves every live table to `<db>_before_<YYYYMMDD_HHMMSS>` and every restored
   table into `<db>`.
6. It writes an audit row (`restore`, resource `database`) and prints the next steps, the carry-over counts, and the
   users whose active or archived state differs between the replaced data and the backup. Those users are left as
   restored: decide on them yourself.

Undo: stop api and media-worker, run `undo-restore`, start them again. The kept schema is swapped back with the same
carry-over: a revocation made after the restore survives the undo. The replaced tables are kept again, so an undo
can itself be undone. `prune` drops kept schemas after `keep_previous_days`. They take as much space as the
database, so on a small disk drop them earlier with `prune` and `MYSETTINGS_BACKUP__KEEP_PREVIOUS_DAYS=0`.

Refusals (exit 3):

| Code | Cause |
|------|-------|
| `not_found` | no such backup |
| `checksum_mismatch` | the file does not match its manifest |
| `unknown_revision` | the backup is newer than this version of the code |
| `connections` | other connections use the database |
| `unsupported_objects` | the database holds objects the swap cannot move |
| `orphan_certificates` | certificates whose user is missing from the backup |
| `nothing_to_undo` | no kept schema to swap back |

## What a rollback does not bring back

- **Media files:** files uploaded after the backup stay in `MEDIA_ROOT` without rows (nothing points to them).
  Rows of the backup whose files were removed later point to missing files (archiving never removes files, so this
  only happens after manual deletion). Back up `MEDIA_ROOT` with the host's file backup, at the same time as the
  database.
- **PKI:** the CA key, the issued packages and the CRL file live in `PKI_DIR` (PKI.md, offline encrypted backup of
  `private/`). Issued `.p12` folders of certificates created after the backup stay on disk; their rows come back as
  revoked.
- **Sessions:** everyone logs in again.
- **Users:** a user deactivated or archived after the backup is active again in the restored data. The restore lists
  such users; deactivate them again if needed.

## Bigger databases, point in time

- A logical dump fits this database: 233 MB on the test server, a few minutes to dump and load. At tens of GB,
  switch to `mariadb-backup`. It makes physical, hot, incremental copies, but a restore replaces the whole data
  directory of the server, needs MariaDB stopped, and has no MySQL equivalent in the same tool.
- A recovery point of minutes needs the binary log (`log_bin`, off on the test server) plus a full backup, and a
  restore replays the log up to a moment. That is a server configuration change and more disk; it is not part of
  this tool.
