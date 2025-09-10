#!/usr/bin/env python3
"""
Initialize signal types and categories in the database
"""

import asyncio
import sys
from pathlib import Path

# Add project root to Python path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from app.backend.config import settings
from app.backend.database import get_session_maker
from app.backend.models.observation import Category, SignalType
from app.backend.utils.logging_config import setup_logging, get_logger
from sqlmodel import select

# Setup logging
setup_logging(
    log_file_path=None,
    log_level="INFO",
    environment="development"
)
logger = get_logger("init_signal_types")

async def initialize_signal_types():
    """Initialize signal types and categories"""
    try:
        session_maker = await get_session_maker()
        async with session_maker() as session:
            # Check if categories exist
            result = await session.exec(select(Category))
            categories = result.all()
            
            if not categories:
                logger.info("Creating default categories...")
                
                # Create default categories
                default_categories = [
                    {"name": "Radio Communications", "alias": "Radio"},
                    {"name": "Digital Signals", "alias": "Digital"},
                    {"name": "Amateur Radio", "alias": "Amateur"},
                    {"name": "Aviation", "alias": "Aviation"},
                    {"name": "Maritime", "alias": "Maritime"},
                    {"name": "Military", "alias": "Military"},
                    {"name": "Unknown", "alias": "Unknown"}
                ]
                
                category_map = {}
                for cat_data in default_categories:
                    category = Category(**cat_data)
                    session.add(category)
                    await session.flush()
                    category_map[cat_data["name"]] = category.id
                    logger.info(f"Created category: {cat_data['name']} (ID: {category.id})")
                
                await session.commit()
                logger.info(f"Created {len(default_categories)} categories")
            else:
                logger.info(f"Found {len(categories)} existing categories")
                category_map = {cat.name: cat.id for cat in categories}
            
            # Check if signal types exist
            result = await session.exec(select(SignalType))
            signal_types = result.all()
            
            if not signal_types:
                logger.info("Creating default signal types...")
                
                # Create default signal types
                default_signal_types = [
                    {"code": "FM", "name": "Frequency Modulation", "category_name": "Radio Communications"},
                    {"code": "AM", "name": "Amplitude Modulation", "category_name": "Radio Communications"},
                    {"code": "SSB", "name": "Single Sideband", "category_name": "Amateur Radio"},
                    {"code": "CW", "name": "Continuous Wave", "category_name": "Amateur Radio"},
                    {"code": "PSK31", "name": "Phase Shift Keying 31", "category_name": "Digital Signals"},
                    {"code": "RTTY", "name": "Radio Teletype", "category_name": "Digital Signals"},
                    {"code": "DMR", "name": "Digital Mobile Radio", "category_name": "Digital Signals"},
                    {"code": "TETRA", "name": "Terrestrial Trunked Radio", "category_name": "Digital Signals"},
                    {"code": "ACARS", "name": "Aircraft Communications Addressing and Reporting System", "category_name": "Aviation"},
                    {"code": "ADS-B", "name": "Automatic Dependent Surveillance-Broadcast", "category_name": "Aviation"},
                    {"code": "AIS", "name": "Automatic Identification System", "category_name": "Maritime"},
                    {"code": "UNKNOWN", "name": "Unknown Signal Type", "category_name": "Unknown"}
                ]
                
                for sig_data in default_signal_types:
                    category_id = category_map.get(sig_data["category_name"])
                    signal_type = SignalType(
                        code=sig_data["code"],
                        name=sig_data["name"],
                        category_id=category_id,
                        description=f"Default {sig_data['name']} signal type",
                        is_active=True,
                        sort_order=len(signal_types)
                    )
                    session.add(signal_type)
                    await session.flush()
                    logger.info(f"Created signal type: {sig_data['code']} - {sig_data['name']} (ID: {signal_type.id})")
                
                await session.commit()
                logger.info(f"Created {len(default_signal_types)} signal types")
            else:
                logger.info(f"Found {len(signal_types)} existing signal types")
            
            logger.info("Modulation types removed from system - no longer needed")
            
            return True
            
    except Exception as e:
        logger.error(f"Failed to initialize signal types: {e}")
        return False

if __name__ == "__main__":
    print("Initializing signal types and categories...")
    success = asyncio.run(initialize_signal_types())
    
    if success:
        print("Signal types and categories initialized successfully!")
        print("You can now create observations with valid signal_type_id values.")
    else:
        print("Failed to initialize signal types and categories")
        sys.exit(1)