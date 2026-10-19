# watch_web

Backend of the Radio Monitoring System: a FastAPI service for posts, observations and their frequency bands,
media (uploads, on-demand thumbnails through imgproxy, video posters and storyboards), role-based access on a
scope tree and client-certificate (mTLS) login behind nginx.

## Stack

- Python 3.13, FastAPI, SQLAlchemy (async), Alembic; MySQL 8 / MariaDB 11 in production, SQLite in tests
- nginx (TLS, client certificates, media offload), imgproxy (thumbnails), ffmpeg (media worker)
- Docker Compose for the application containers; database, nginx and media storage stay on the host

## Development

```bash
python -m venv venv
venv/Scripts/pip install -r requirements.txt        # Windows; venv/bin/pip elsewhere
venv/Scripts/python -m app.main                       # API with app/config.toml (development defaults)
venv/Scripts/python -m pytest                         # in-memory SQLite, no MySQL needed
```

The API applies migrations and seeds the admin account on start. Migration commands:
`python -m app.backend.cli.migrations_manager {migrate,revision,current,history,downgrade,seed}`.

## Documentation

| File | Contents |
| --- | --- |
| [API.md](API.md) | Client contract: errors (RFC 9457), localization keys, versioning |
| [WEB_TOOLING.md](WEB_TOOLING.md) | Web client tooling: openapi-typescript, typed translations |
| [UPLOADS.md](UPLOADS.md) | Resumable upload protocol |
| [ENV.md](ENV.md) | Settings and environment variables |
| [DEPLOYMENT.md](DEPLOYMENT.md) | Production topology, Docker Compose, nginx, systemd |
| [PKI.md](PKI.md) | Client certificate authority, issuing and revoking certificates, CRL |
| [CHANGELOG.md](CHANGELOG.md) | API and database changes |

The OpenAPI document of each API version is committed in `api-contract/v1/openapi.json` and served at
`/api/v1/openapi.json` (viewers at `/api/v1/docs` and `/api/v1/redoc`).
