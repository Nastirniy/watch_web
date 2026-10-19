"""A command line tool closes the database pool before asyncio.run closes its event loop.

Left to the garbage collector, the pool's connections were closed after the loop was gone: init_user ended with
"Event loop is closed" although the user was created (found on the test server, 2026-10-19). cli/pki.py and
cli/media_tools.py close it in a finally; these are the commands that did not.
"""
import asyncio
import sys

import pytest

from app.backend.cli import init_user, migrations_manager


def _spy_close(monkeypatch, module, events):
    async def close_db():
        asyncio.get_running_loop()   # raises outside a running loop: the pool must close while the loop runs
        events.append("closed")

    monkeypatch.setattr(module, "close_db", close_db)


@pytest.mark.parametrize("argv", [
    ["init_user", "--username", "admin1", "--alias", "Admin", "--user-type", "admin"],
    ["init_user", "cert.pem", "--label", "Laptop"],   # interactive: prompts for the rest
])
def test_init_user_closes_the_pool(monkeypatch, argv):
    events = []

    async def create_user(*args, **kwargs):
        events.append("created")

    async def interactive_create(*args, **kwargs):
        events.append("created")

    monkeypatch.setattr(init_user, "create_user", create_user)
    monkeypatch.setattr(init_user, "interactive_create", interactive_create)
    _spy_close(monkeypatch, init_user, events)
    monkeypatch.setattr(sys, "argv", argv)
    init_user.main()
    assert events == ["created", "closed"]


def test_init_user_closes_the_pool_when_the_command_fails(monkeypatch):
    events = []

    async def create_user(*args, **kwargs):
        sys.exit(1)

    monkeypatch.setattr(init_user, "create_user", create_user)
    _spy_close(monkeypatch, init_user, events)
    monkeypatch.setattr(sys, "argv", ["init_user", "--username", "admin1", "--alias", "A", "--user-type", "admin"])
    with pytest.raises(SystemExit):
        init_user.main()
    assert events == ["closed"]


def test_seed_closes_the_pool(monkeypatch):
    events = []

    async def seed_database():
        events.append("seeded")

    monkeypatch.setattr(migrations_manager, "seed_database", seed_database)
    _spy_close(monkeypatch, migrations_manager, events)
    monkeypatch.setattr(sys, "argv", ["migrations_manager", "seed"])
    migrations_manager.main()
    assert events == ["seeded", "closed"]
