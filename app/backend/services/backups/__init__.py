"""Database backups (docs/superpowers/specs/2026-10-22-db-backups-design.md, BACKUPS.md).

The catalog is the backup folders on disk (manifest.json), never the database: a restore must not erase the list of
backups. Nothing here reaches FastAPI or app.backend.api; the CLI (cli/db_backup.py) does the work, the API reads
the catalog (catalog.py) and the database status.

- manifest.py: the manifest of one backup folder
- retention.py: which backups to keep (pure)
- catalog.py: the backup folders and their status (read-only)
- dump.py: create a backup (mariadb-dump), copy it, prune
- carry_over.py: what a restore keeps from the data it replaces (revocations, certificates, CRL number, sessions)
- restore.py, schemas.py: restore, undo and deep verify (staging schema, one RENAME TABLE)
"""
