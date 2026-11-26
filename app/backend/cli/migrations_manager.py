#!/usr/bin/env python3
"""
Database Migration Management Utility

Usage:
    python -m app.backend.cli.migrations_manager init       - Initialize migrations (create first migration)
    python -m app.backend.cli.migrations_manager migrate    - Apply all pending migrations the way the api's start
                                                              does: schema check, schema lock, upgrade, seeding
                                                              (exit 3 when this release must not run on the schema)
    python -m app.backend.cli.migrations_manager revision   - Create new migration from model changes
    python -m app.backend.cli.migrations_manager downgrade  - Downgrade to previous migration
    python -m app.backend.cli.migrations_manager current    - Show current migration
    python -m app.backend.cli.migrations_manager history    - Show migration history
    python -m app.backend.cli.migrations_manager seed       - Run database seeding
"""

import asyncio
import os
import sys
import argparse
from pathlib import Path

from alembic.config import Config
from alembic import command
from app.backend.bootstrap.seed import seed_database
from app.backend.database import close_db, create_database_if_not_exists
from app.backend.utils.logging_config import get_logger

_logger = get_logger("migrations")


def get_alembic_config():
    """Get alembic configuration"""
    # Get the project root directory (where alembic.ini is located)
    project_root_dir = Path(__file__).resolve().parents[3]
    alembic_ini_path = project_root_dir / "alembic.ini"
    alembic_cfg = Config(str(alembic_ini_path))
    return alembic_cfg


async def init_migrations():
    """Initialize migrations - create first migration from current models"""
    _logger.info("Initializing migrations...")
    
    # Ensure database exists
    await create_database_if_not_exists()
    
    alembic_cfg = get_alembic_config()
    
    try:
        # Create initial migration
        command.revision(alembic_cfg, autogenerate=True, message="Initial migration")
        _logger.info("Initial migration created successfully")
        
        # Apply the migration
        command.upgrade(alembic_cfg, "head")
        _logger.info("Initial migration applied successfully")
        
    except Exception as e:
        _logger.error(f"Failed to initialize migrations: {e}")
        raise


async def migrate_locked():
    """The api's start without the api (bootstrap/startup.run_locked_start, role migrate): the schema check, the
    schema lock every api worker takes, upgrade to this release's head, seeding and the start checks. A starting
    api and this command therefore never run alembic at once (RELEASE.md, deploy step 2)."""
    from app.backend.bootstrap.startup import run_locked_start
    try:
        return await run_locked_start("migrate")
    finally:
        await close_db()


def migrate():
    """Apply all pending migrations (exit 3: this release must not run on the database's schema)"""
    if os.environ.get("WATCH_WEB_ALEMBIC_URL"):
        # A one-off database named by URL (tests, checks; migrations/env.py): plain alembic, no schema check, lock or
        # seeding, which work on the configured database only
        _logger.info("WATCH_WEB_ALEMBIC_URL set: plain alembic upgrade of that database")
        command.upgrade(get_alembic_config(), "head")
        _logger.info("Migrations applied successfully")
        return
    from app.backend.bootstrap.schema_range import SchemaOutOfRange
    try:
        asyncio.run(migrate_locked())
    except SchemaOutOfRange:
        sys.exit(3)  # the one line is logged already


def create_revision(message: str = None):
    """Create new migration from model changes"""
    if not message:
        message = input("Enter migration message: ").strip()
    
    if not message:
        _logger.error("Migration message is required")
        return
    
    _logger.info(f"Creating migration: {message}")
    
    alembic_cfg = get_alembic_config()
    
    try:
        command.revision(alembic_cfg, autogenerate=True, message=message)
        _logger.info("Migration created successfully")
    except Exception as e:
        _logger.error(f"Failed to create migration: {e}")
        raise


def downgrade_migration(revision: str = "-1"):
    """Downgrade to previous or specific migration"""
    _logger.info(f"Downgrading to: {revision}")
    
    alembic_cfg = get_alembic_config()
    
    try:
        command.downgrade(alembic_cfg, revision)
        _logger.info("Migration downgraded successfully")
    except Exception as e:
        _logger.error(f"Failed to downgrade migration: {e}")
        raise


def show_current():
    """Show current migration"""
    alembic_cfg = get_alembic_config()
    
    try:
        command.current(alembic_cfg)
    except Exception as e:
        _logger.error(f"Failed to show current migration: {e}")
        raise


def show_history():
    """Show migration history"""
    alembic_cfg = get_alembic_config()
    
    try:
        command.history(alembic_cfg)
    except Exception as e:
        _logger.error(f"Failed to show migration history: {e}")
        raise


async def run_seed():
    """Run database seeding"""
    _logger.info("Running database seeding...")
    
    try:
        await seed_database()
        _logger.info("Database seeding completed successfully")
    except Exception as e:
        _logger.error(f"Failed to seed database: {e}")
        raise
    finally:
        # Close the pool while the event loop still runs (asyncio.run in main), not after it in the garbage collector
        await close_db()


def main():
    """Main CLI interface"""
    parser = argparse.ArgumentParser(description="Database Migration Management")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")
    
    # Init command
    subparsers.add_parser("init", help="Initialize migrations")
    
    # Migrate command
    subparsers.add_parser("migrate", help="Apply all pending migrations")
    
    # Revision command
    revision_parser = subparsers.add_parser("revision", help="Create new migration")
    revision_parser.add_argument("-m", "--message", help="Migration message")
    
    # Downgrade command  
    downgrade_parser = subparsers.add_parser("downgrade", help="Downgrade migration")
    downgrade_parser.add_argument("revision", nargs="?", default="-1", help="Revision to downgrade to")
    
    # Current command
    subparsers.add_parser("current", help="Show current migration")
    
    # History command
    subparsers.add_parser("history", help="Show migration history")
    
    # Seed command
    subparsers.add_parser("seed", help="Run database seeding")
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return
    
    try:
        if args.command == "init":
            asyncio.run(init_migrations())
        elif args.command == "migrate":
            migrate()
        elif args.command == "revision":
            create_revision(args.message)
        elif args.command == "downgrade":
            downgrade_migration(args.revision)
        elif args.command == "current":
            show_current()
        elif args.command == "history":
            show_history()
        elif args.command == "seed":
            asyncio.run(run_seed())
        else:
            parser.print_help()
            
    except Exception as e:
        _logger.error(f"Command failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()