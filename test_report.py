"""
Test script for POST /api/v1/observations/report
Uses default admin credentials from app/backend/config.py (dev mode only).
Generates both report types (default frequencies and video frequencies).
"""

import requests

BASE = "http://localhost:8000/api/v1"

LOGIN_PAYLOAD = {
    "certificate_cn":          "admin@radiomonitoring.local",
    "certificate_serial":      "ABC123DEF456",
    "certificate_fingerprint": "12:34:56:78:90:AB:CD:EF:12:34:56:78:90:AB:CD:EF:12:34:56:78",
}


def get_token() -> str:
    r = requests.post(f"{BASE}/auth/login", json=LOGIN_PAYLOAD)
    r.raise_for_status()
    print("Login OK")
    return r.json()["access_token"]


def download_report(token: str, report_type: str, filename: str):
    r = requests.post(
        f"{BASE}/observations/report",
        json={"report_type": report_type},
        headers={"Authorization": f"Bearer {token}"},
    )
    r.raise_for_status()
    with open(filename, "wb") as f:
        f.write(r.content)
    print(f"Saved [{report_type}] -> {filename}  ({len(r.content):,} bytes)")


def main():
    token = get_token()
    download_report(token, "default", "report_default_freq.xlsx")
    download_report(token, "video",   "report_video_freq.xlsx")


if __name__ == "__main__":
    main()