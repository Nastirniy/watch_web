#!/usr/bin/env python3
"""
Database Migration Management Utility

Usage:
    python migrations.py init          - Initialize migrations (create first migration)
    python migrations.py migrate       - Apply all pending migrations
    python migrations.py revision      - Create new migration from model changes
    python migrations.py downgrade     - Downgrade to previous migration
    python migrations.py current       - Show current migration
    python migrations.py history       - Show migration history
    python migrations.py seed          - Run database seeding
"""

import asyncio
import sys
import argparse
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from alembic.config import Config
from alembic import command
from app.backend.database import seed_database, create_database_if_not_exists
from app.backend.utils.logging_config import get_logger

logger = get_logger("migrations")


def get_alembic_config():
    """Get alembic configuration"""
    # Get the project root directory (where alembic.ini is located)
    project_root_dir = Path(__file__).parent.parent.parent
    alembic_ini_path = project_root_dir / "alembic.ini"
    alembic_cfg = Config(str(alembic_ini_path))
    return alembic_cfg


async def init_migrations():
    """Initialize migrations - create first migration from current models"""
    logger.info("Initializing migrations...")
    
    # Ensure database exists
    await create_database_if_not_exists()
    
    alembic_cfg = get_alembic_config()
    
    try:
        # Create initial migration
        command.revision(alembic_cfg, autogenerate=True, message="Initial migration")
        logger.info("✅ Initial migration created successfully")
        
        # Apply the migration
        command.upgrade(alembic_cfg, "head")
        logger.info("✅ Initial migration applied successfully")
        
    except Exception as e:
        logger.error(f"❌ Failed to initialize migrations: {e}")
        raise


def migrate():
    """Apply all pending migrations"""
    logger.info("Applying migrations...")
    
    alembic_cfg = get_alembic_config()
    
    try:
        command.upgrade(alembic_cfg, "head")
        logger.info("✅ Migrations applied successfully")
    except Exception as e:
        logger.error(f"❌ Failed to apply migrations: {e}")
        raise


def create_revision(message: str = None):
    """Create new migration from model changes"""
    if not message:
        message = input("Enter migration message: ").strip()
    
    if not message:
        logger.error("❌ Migration message is required")
        return
    
    logger.info(f"Creating migration: {message}")
    
    alembic_cfg = get_alembic_config()
    
    try:
        command.revision(alembic_cfg, autogenerate=True, message=message)
        logger.info("✅ Migration created successfully")
    except Exception as e:
        logger.error(f"❌ Failed to create migration: {e}")
        raise


def downgrade_migration(revision: str = "-1"):
    """Downgrade to previous or specific migration"""
    logger.info(f"Downgrading to: {revision}")
    
    alembic_cfg = get_alembic_config()
    
    try:
        command.downgrade(alembic_cfg, revision)
        logger.info("✅ Migration downgraded successfully")
    except Exception as e:
        logger.error(f"❌ Failed to downgrade migration: {e}")
        raise


def show_current():
    """Show current migration"""
    alembic_cfg = get_alembic_config()
    
    try:
        command.current(alembic_cfg)
    except Exception as e:
        logger.error(f"❌ Failed to show current migration: {e}")
        raise


def show_history():
    """Show migration history"""
    alembic_cfg = get_alembic_config()
    
    try:
        command.history(alembic_cfg)
    except Exception as e:
        logger.error(f"❌ Failed to show migration history: {e}")
        raise


async def run_seed():
    """Run database seeding"""
    logger.info("Running database seeding...")
    
    try:
        await seed_database()
        logger.info("✅ Database seeding completed successfully")
    except Exception as e:
        logger.error(f"❌ Failed to seed database: {e}")
        raise


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
        logger.error(f"Command failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()