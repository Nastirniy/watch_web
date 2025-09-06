#!/usr/bin/env python3
"""
Test script for user operations: admin auth, create user, list users
"""

import requests
import json
from datetime import datetime
from typing import Optional


class APIClient:
    def __init__(self, base_url: str = "http://127.0.0.1:60800/api/v1"):
        self.base_url = base_url
        self.token: Optional[str] = None
        self.session = requests.Session()
    
    def login_admin(self) -> bool:
        """Login as admin using PKI credentials from config"""
        login_data = {
            "certificate_cn": "ADMIN_CN",
            "certificate_serial": "ADMIN_SERIAL_001", 
            "certificate_fingerprint": "ADMIN_FINGERPRINT_001"
        }
        
        try:
            response = self.session.post(
                f"{self.base_url}/auth/login",
                json=login_data
            )
            response.raise_for_status()
            
            result = response.json()
            self.token = result["access_token"]
            self.session.headers.update({
                "Authorization": f"Bearer {self.token}"
            })
            
            print(f"[+] Admin login successful. User: {result['username']} (ID: {result['user_id']})")
            return True
            
        except requests.exceptions.RequestException as e:
            print(f"[-] Admin login failed: {e}")
            return False
    
    def create_user(self, username: str) -> Optional[dict]:
        """Create a new user"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        user_data = {
            "username": f"{username}_{timestamp}",
            "username_alias": f"{username.title()} User",
            "pki_certificate_cn": f"CN_{username.upper()}_{timestamp}",
            "pki_certificate_serial": f"SERIAL_{username.upper()}_{timestamp}",
            "pki_certificate_fingerprint": f"FINGERPRINT_{username.upper()}_{timestamp}",
            "post_id": None
        }
        
        try:
            response = self.session.post(
                f"{self.base_url}/users/",
                json=user_data
            )
            response.raise_for_status()
            
            result = response.json()
            print(f"[+] User created: {result['username']} (ID: {result['id']})")
            return result
            
        except requests.exceptions.RequestException as e:
            print(f"[-] User creation failed: {e}")
            if hasattr(e.response, 'text'):
                print(f"   Error details: {e.response.text}")
            return None
    
    def list_users(self, limit: int = 10) -> Optional[list]:
        """List all users"""
        try:
            response = self.session.get(
                f"{self.base_url}/users/",
                params={"limit": limit}
            )
            response.raise_for_status()
            
            users = response.json()
            print(f"[+] Retrieved {len(users)} users:")
            
            for user in users:
                status = "Active" if user['is_active'] else "Inactive"
                print(f"   - {user['username']} ({user['username_alias']}) - {status} - ID: {user['id']}")
            
            return users
            
        except requests.exceptions.RequestException as e:
            print(f"[-] Failed to list users: {e}")
            if hasattr(e.response, 'text'):
                print(f"   Error details: {e.response.text}")
            return None
    
    def get_current_user_info(self) -> Optional[dict]:
        """Get current authenticated user info"""
        try:
            response = self.session.get(f"{self.base_url}/auth/me")
            response.raise_for_status()
            
            result = response.json()
            print(f"[+] Current user: {result['username']} ({result['username_alias']})")
            return result
            
        except requests.exceptions.RequestException as e:
            print(f"[-] Failed to get user info: {e}")
            return None


def main():
    """Main test function"""
    print("=== User Operations Test ===\n")
    
    client = APIClient()
    
    # Step 1: Admin login
    print("1. Authenticating as admin...")
    if not client.login_admin():
        print("Cannot proceed without admin authentication")
        return
    
    # Step 2: Get current user info
    print("\n2. Getting current user info...")
    client.get_current_user_info()
    
    # Step 3: List existing users
    print("\n3. Listing existing users...")
    client.list_users()
    
    # Step 4: Create new user
    print("\n4. Creating new user...")
    new_user = client.create_user("testuser")
    
    # Step 5: List users again to see the new user
    print("\n5. Listing users after creation...")
    client.list_users()
    
    print("\n=== Test Complete ===")


if __name__ == "__main__":
    main()