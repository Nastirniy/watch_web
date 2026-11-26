"""Paths the tests read, independent of the folder a test lives in (tests/<area>/test_*.py)"""
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_DIR.parent
APP_DIR = PROJECT_ROOT / "app"
BACKEND_DIR = APP_DIR / "backend"
FIXTURE_FILES = TESTS_DIR / "fixtures" / "files"
