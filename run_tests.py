#!/usr/bin/env python3
"""
Test runner script for Radio Monitoring System API tests

Usage:
    python run_tests.py                    # Run all tests
    python run_tests.py --auth             # Run only auth tests
    python run_tests.py --integration      # Run only integration tests
    python run_tests.py --coverage         # Run with coverage report
    python run_tests.py --html             # Generate HTML report
"""

import sys
import subprocess
import argparse
from pathlib import Path


def run_command(cmd, description):
    """Run a command and handle errors"""
    print(f"\n{'='*50}")
    print(f"🔄 {description}")
    print(f"{'='*50}")
    
    try:
        result = subprocess.run(cmd, shell=True, check=True)
        print(f"✅ {description} completed successfully")
        return True
    except subprocess.CalledProcessError as e:
        print(f"❌ {description} failed with exit code {e.returncode}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Run API tests for Radio Monitoring System")
    parser.add_argument("--auth", action="store_true", help="Run only authentication tests")
    parser.add_argument("--api", action="store_true", help="Run only API endpoint tests")
    parser.add_argument("--integration", action="store_true", help="Run only integration tests")
    parser.add_argument("--unit", action="store_true", help="Run only unit tests")
    parser.add_argument("--coverage", action="store_true", help="Run with coverage report")
    parser.add_argument("--html", action="store_true", help="Generate HTML test report")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    parser.add_argument("--parallel", "-n", type=int, help="Number of parallel workers")
    parser.add_argument("--markers", "-m", help="Run tests with specific markers")
    parser.add_argument("--file", "-f", help="Run specific test file")
    
    args = parser.parse_args()
    
    # Base pytest command
    cmd_parts = ["python", "-m", "pytest"]
    
    # Add test selection based on arguments
    if args.auth:
        cmd_parts.extend(["-m", "auth"])
    elif args.api:
        cmd_parts.extend(["-m", "api"])
    elif args.integration:
        cmd_parts.extend(["-m", "integration"])
    elif args.unit:
        cmd_parts.extend(["-m", "unit"])
    elif args.markers:
        cmd_parts.extend(["-m", args.markers])
    elif args.file:
        cmd_parts.append(args.file)
    else:
        cmd_parts.append("tests/")
    
    # Add coverage if requested
    if args.coverage:
        cmd_parts.extend([
            "--cov=app",
            "--cov-report=term-missing",
            "--cov-report=html:htmlcov",
            "--cov-fail-under=80"
        ])
    
    # Add HTML report if requested
    if args.html:
        cmd_parts.extend([
            "--html=reports/test_report.html",
            "--self-contained-html"
        ])
    
    # Add verbosity
    if args.verbose:
        cmd_parts.append("-v")
    else:
        cmd_parts.append("-q")
    
    # Add parallel execution
    if args.parallel:
        cmd_parts.extend(["-n", str(args.parallel)])
    
    # Create reports directory if needed
    reports_dir = Path("reports")
    reports_dir.mkdir(exist_ok=True)
    
    # Build final command
    cmd = " ".join(cmd_parts)
    
    print("🚀 Radio Monitoring System API Test Suite")
    print(f"📋 Command: {cmd}")
    
    # Run the tests
    success = run_command(cmd, "Running API Tests")
    
    if success:
        print(f"\n🎉 All tests completed successfully!")
        if args.coverage:
            print(f"📊 Coverage report available at: htmlcov/index.html")
        if args.html:
            print(f"📋 Test report available at: reports/test_report.html")
    else:
        print(f"\n💥 Some tests failed. Check the output above for details.")
        sys.exit(1)


if __name__ == "__main__":
    main()