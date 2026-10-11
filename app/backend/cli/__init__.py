"""Command-line tools, run from the project root as modules:

    python -m app.backend.cli.migrations_manager {init,migrate,revision,downgrade,current,history,seed}
    python -m app.backend.cli.init_user /path/to/cert.pem [--username ...] | --list-roles
    python -m app.backend.cli.media_tools {cleanup-legacy-thumbnails,reconcile-posters,poster-status,...}
    python -m app.backend.cli.api_contract   (rewrites api-contract/vN/openapi.json)

The media worker is a long-running process, not a tool: python -m app.backend.workers.media_worker.
"""
