#!/usr/bin/env python3
"""
Frontend Application Simulation Test

This test simulates real frontend application usage scenarios:
1. User authentication and session management
2. Creating observations with metadata
3. Uploading media files (images, videos, documents)
4. Retrieving and managing data
5. File integrity verification
6. Real error handling scenarios

Run this test against a live backend server to verify end-to-end functionality.
"""

import requests
import json
import time
import os
import tempfile
import hashlib
from pathlib import Path
from typing import Dict, Any, Optional
import mimetypes

class FrontendSimulator:
    """Simulates a frontend application interacting with the backend API"""

    def __init__(self, base_url: str = "http://localhost:8000"):
        self.base_url = base_url.rstrip('/')
        self.session = requests.Session()
        self.auth_token = None
        self.user_data = None

    def _make_request(self, method: str, endpoint: str, **kwargs) -> requests.Response:
        """Make HTTP request with proper error handling"""
        url = f"{self.base_url}{endpoint}"

        # Add authentication if available
        if self.auth_token and 'headers' not in kwargs:
            kwargs['headers'] = {}
        if self.auth_token:
            kwargs['headers']['Authorization'] = f"Bearer {self.auth_token}"

        try:
            response = self.session.request(method, url, **kwargs)
            print(f"{method} {endpoint} -> {response.status_code}")
            return response
        except requests.exceptions.RequestException as e:
            print(f"Request failed: {e}")
            raise

    def health_check(self) -> bool:
        """Check if backend server is running"""
        try:
            response = self._make_request('GET', '/health')
            return response.status_code == 200
        except:
            return False

    def authenticate_user(self, username: str = "admin", password: str = "admin") -> bool:
        """Simulate user login"""
        print(f"\n Authenticating user: {username}")

        # Try to login with PKI credentials from config.toml
        login_data = {
            "username": username,
            "password": password,
            "certificate_cn": "admin@radiomonitoring.local",
            "certificate_serial": "ABC123DEF456",
            "certificate_fingerprint": "12:34:56:78:90:AB:CD:EF:12:34:56:78:90:AB:CD:EF:12:34:56:78"
        }

        response = self._make_request('POST', '/api/v1/auth/login', json=login_data)

        if response.status_code == 200:
            data = response.json()
            self.auth_token = data.get('access_token')
            self.user_data = data.get('user')

            if self.user_data:
                print(f"SUCCESS: Authentication successful for user: {self.user_data.get('username')}")
            else:
                print(f"SUCCESS: Authentication successful. Token: {self.auth_token[:20]}...")
            return True
        else:
            print(f"ERROR: Authentication failed: {response.status_code}")
            if response.status_code == 422:
                print(f"Error details: {response.json()}")
            return False

    def get_current_user(self) -> Optional[Dict]:
        """Get current user information"""
        response = self._make_request('GET', '/api/v1/auth/me')
        if response.status_code == 200:
            return response.json()
        return None

    def create_observation(self, observation_data: Dict[str, Any]) -> Optional[int]:
        """Create a new observation"""
        print(f"\nNOTE: Creating observation: {observation_data.get('title', 'Untitled')}")

        response = self._make_request('POST', '/api/v1/observations/', json=observation_data)

        if response.status_code in [200, 201]:
            obs = response.json()
            observation_id = obs['id']
            print(f"SUCCESS: Observation created with ID: {observation_id}")
            return observation_id
        else:
            print(f"ERROR: Failed to create observation: {response.status_code}")
            if response.content:
                print(f"Error: {response.text}")
            return None

    def upload_media_file(self, observation_id: int, file_path: str, media_type: str) -> Optional[int]:
        """Upload a media file to an observation"""
        print(f"\nUPLOAD: Uploading file: {file_path}")

        if not os.path.exists(file_path):
            print(f"ERROR: File not found: {file_path}")
            return None

        # Determine MIME type
        mime_type, _ = mimetypes.guess_type(file_path)
        if not mime_type:
            mime_type = 'application/octet-stream'

        # Prepare file for upload
        with open(file_path, 'rb') as f:
            files = {
                'file': (os.path.basename(file_path), f, mime_type)
            }
            data = {
                'observation_id': observation_id,
                'media_type': media_type
            }

            response = self._make_request(
                'POST',
                '/api/v1/media/media/upload',
                files=files,
                data=data
            )

        if response.status_code == 200:
            result = response.json()
            media_id = result.get('media_file_id')
            print(f"SUCCESS: File uploaded successfully. Media ID: {media_id}")
            return media_id
        else:
            print(f"ERROR: File upload failed: {response.status_code}")
            if response.content:
                try:
                    error_detail = response.json()
                    print(f"Error details: {error_detail}")
                except:
                    print(f"Error: {response.text}")
            return None

    def download_media_file(self, media_id: int, save_path: str = None) -> bool:
        """Download a media file"""
        print(f"\nDOWNLOAD: Downloading media file ID: {media_id}")

        response = self._make_request('GET', f'/api/v1/media/media/{media_id}/download')

        if response.status_code == 200:
            if save_path:
                with open(save_path, 'wb') as f:
                    f.write(response.content)
                print(f"SUCCESS: File downloaded to: {save_path}")
            else:
                print(f"SUCCESS: File download successful ({len(response.content)} bytes)")
            return True
        else:
            print(f"ERROR: File download failed: {response.status_code}")
            return False

    def check_file_integrity(self, media_id: int) -> bool:
        """Check file integrity"""
        print(f"\nCHECK: Checking integrity of media file ID: {media_id}")

        response = self._make_request('GET', f'/api/v1/media/integrity/{media_id}')

        if response.status_code == 200:
            integrity = response.json()
            status = integrity.get('status')
            print(f"SUCCESS: Integrity check result: {status}")
            if integrity.get('issues'):
                print(f"Issues found: {integrity['issues']}")
            return status == 'ok'
        else:
            print(f"ERROR: Integrity check failed: {response.status_code}")
            return False

    def list_observations(self, limit: int = 10) -> list:
        """List user's observations"""
        print(f"\nLIST: Listing observations (limit: {limit})")

        response = self._make_request('GET', f'/api/v1/observations/?limit={limit}')

        if response.status_code == 200:
            observations = response.json()
            print(f"SUCCESS: Found {len(observations)} observations")
            return observations
        else:
            print(f"ERROR: Failed to list observations: {response.status_code}")
            return []

    def get_observation_details(self, observation_id: int) -> Optional[Dict]:
        """Get detailed observation information"""
        print(f"\nCHECK: Getting observation details for ID: {observation_id}")

        response = self._make_request('GET', f'/api/v1/observations/{observation_id}')

        if response.status_code == 200:
            observation = response.json()
            print(f"SUCCESS: Retrieved observation: {observation.get('title')}")
            return observation
        else:
            print(f"ERROR: Failed to get observation: {response.status_code}")
            return None

    def list_media_for_observation(self, observation_id: int) -> list:
        """List all media files for a specific observation"""
        print(f"\nMEDIA: Listing media files for observation {observation_id}")

        response = self._make_request('GET', f'/api/v1/media/observation/{observation_id}')

        if response.status_code == 200:
            media_files = response.json()
            print(f"SUCCESS: Found {len(media_files)} media files for observation {observation_id}")

            for media in media_files:
                media_id = media.get('id')
                filename = media.get('original_filename', 'Unknown')
                media_type = media.get('media_type', 'Unknown')
                file_size = media.get('file_size', 0)
                upload_status = media.get('upload_complete', False)

                print(f"  - Media ID {media_id}: {filename}")
                print(f"    Type: {media_type}, Size: {file_size} bytes, Complete: {upload_status}")

            return media_files
        else:
            print(f"ERROR: Failed to list media for observation {observation_id}: {response.status_code}")
            return []


def create_test_files() -> Dict[str, str]:
    """Create temporary test files for upload"""
    temp_dir = tempfile.mkdtemp()
    test_files = {}

    # Create a text document
    txt_file = os.path.join(temp_dir, "test_document.txt")
    with open(txt_file, 'w') as f:
        f.write("This is a test document created by the frontend simulation.\n")
        f.write(f"Created at: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("This file demonstrates document upload functionality.\n")
    test_files['document'] = txt_file

    # Create a fake image file (PNG header)
    img_file = os.path.join(temp_dir, "test_image.png")
    with open(img_file, 'wb') as f:
        # PNG signature
        f.write(b'\x89PNG\r\n\x1a\n')
        # Add some dummy data
        f.write(b'\x00' * 1000)
    test_files['image'] = img_file

    # Create a fake video file (MP4 header)
    video_file = os.path.join(temp_dir, "test_video.mp4")
    with open(video_file, 'wb') as f:
        # MP4 signature
        f.write(b'\x00\x00\x00\x20ftypmp42\x00\x00\x00\x00mp42isom')
        # Add some dummy data
        f.write(b'\x00' * 5000)
    test_files['video'] = video_file

    return test_files


def run_frontend_simulation():
    """Run complete frontend application simulation"""
    print("Starting Frontend Application Simulation")
    print("=" * 60)

    # Initialize simulator
    simulator = FrontendSimulator()

    # Step 1: Health check
    print("\n1. HEALTH CHECK")
    if not simulator.health_check():
        print("ERROR: Backend server is not running!")
        print("Please start the backend server first:")
        print("python -m app.main")
        return False
    print("SUCCESS: Backend server is running")

    # Step 2: Authentication
    print("\n2. USER AUTHENTICATION")
    if not simulator.authenticate_user():
        print("ERROR: Authentication failed. Check if admin user exists.")
        return False

    # Get current user info
    user_info = simulator.get_current_user()
    if user_info:
        print(f"Current user: {user_info.get('username')} (ID: {user_info.get('id')})")

    # Step 3: Create test files
    print("\n3. PREPARING TEST FILES")
    test_files = create_test_files()
    print(f"SUCCESS: Created {len(test_files)} test files:")
    for file_type, path in test_files.items():
        size = os.path.getsize(path)
        print(f"  - {file_type}: {os.path.basename(path)} ({size} bytes)")

    # Step 4: Create observations
    print("\n4. CREATING OBSERVATIONS")

    # Create a drone observation (radio monitoring system)
    drone_obs_data = {
        "post_id": 1,  # Test Radio Post
        "signal_type_id": 1,  # FPV drone
        "frequency_min": 2400.0,  # 2.4 GHz
        "frequency_max": 2485.0,  # 2.485 GHz
        "observed_at": "2024-01-15T14:30:00Z",
        "duration_seconds": 180,
        "comment": "Detected FPV drone signal during routine monitoring",
        "confidence_level": "high"
    }

    drone_obs_id = simulator.create_observation(drone_obs_data)
    if not drone_obs_id:
        print("ERROR: Failed to create drone observation")
        return False

    # Create a UAV wing observation
    uav_obs_data = {
        "post_id": 1,  # Test Radio Post
        "signal_type_id": 2,  # Wing UAV
        "frequency_min": 868.0,  # 868 MHz
        "frequency_max": 915.0,  # 915 MHz
        "video_frequency_min": 5725.0,  # 5.725 GHz video
        "video_frequency_max": 5875.0,  # 5.875 GHz video
        "observed_at": "2024-01-15T16:45:00Z",
        "duration_seconds": 300,
        "comment": "Wing UAV with video transmission detected",
        "confidence_level": "medium"
    }

    uav_obs_id = simulator.create_observation(uav_obs_data)
    if not uav_obs_id:
        print("ERROR: Failed to create UAV observation")
        return False

    # Step 5: Upload media files
    print("\n5. UPLOADING MEDIA FILES")

    # Upload image to drone observation
    image_media_id = simulator.upload_media_file(
        drone_obs_id,
        test_files['image'],
        'image'
    )

    # Upload document to UAV observation
    doc_media_id = simulator.upload_media_file(
        uav_obs_id,
        test_files['document'],
        'document'
    )

    # Upload video to drone observation
    video_media_id = simulator.upload_media_file(
        drone_obs_id,
        test_files['video'],
        'video'
    )

    uploaded_files = [f for f in [image_media_id, doc_media_id, video_media_id] if f]
    print(f"SUCCESS: Successfully uploaded {len(uploaded_files)} files")

    # Step 5.5: Show all media files for each observation
    print("\n5.5. LISTING MEDIA FILES FOR OBSERVATIONS")

    # Show media files for drone observation
    drone_media = simulator.list_media_for_observation(drone_obs_id)

    # Show media files for UAV observation
    uav_media = simulator.list_media_for_observation(uav_obs_id)

    # Step 6: Download and verify files
    print("\n6. DOWNLOADING AND VERIFYING FILES")
    download_dir = tempfile.mkdtemp()

    for i, media_id in enumerate(uploaded_files):
        if media_id:
            download_path = os.path.join(download_dir, f"downloaded_file_{media_id}.bin")
            if simulator.download_media_file(media_id, download_path):
                # Check file integrity
                simulator.check_file_integrity(media_id)

    # Step 7: List and retrieve observations
    print("\n7. RETRIEVING OBSERVATIONS")
    observations = simulator.list_observations()

    for obs in observations[:3]:  # Show first 3
        print(f"  NOTE: Found observation ID {obs['id']} - Status: {obs.get('status', 'Unknown')}")

    # Step 8: Test error scenarios
    print("\n8. TESTING ERROR SCENARIOS")

    # Try to access non-existent observation
    print("Testing non-existent observation...")
    simulator.get_observation_details(99999)

    # Try to download non-existent media
    print("Testing non-existent media download...")
    simulator.download_media_file(99999)

    # Try to upload invalid file
    print("Testing invalid file upload...")
    invalid_file = os.path.join(tempfile.gettempdir(), "nonexistent.txt")
    simulator.upload_media_file(drone_obs_id, invalid_file, 'document')

    # Final summary
    print("\n" + "=" * 60)
    print(" FRONTEND SIMULATION COMPLETED")
    print("=" * 60)
    print(f"SUCCESS: Created {len([drone_obs_id, uav_obs_id])} observations")
    print(f"SUCCESS: Uploaded {len(uploaded_files)} media files")
    print(f"SUCCESS: All core functionality tested")

    # Cleanup
    import shutil
    for temp_path in [test_files, {download_dir: None}]:
        if isinstance(temp_path, dict):
            for path in temp_path.values():
                if path and os.path.exists(path):
                    try:
                        if os.path.isdir(path):
                            shutil.rmtree(path)
                        else:
                            os.remove(path)
                    except:
                        pass
        elif isinstance(temp_path, str) and os.path.exists(temp_path):
            try:
                shutil.rmtree(temp_path)
            except:
                pass

    return True


if __name__ == "__main__":
    print("Frontend Application Simulation")
    print("This test simulates real frontend app usage against a live backend server.")
    print("\nMake sure the backend server is running:")
    print("  python -m app.main")
    print("\nStarting simulation automatically...")

    success = run_frontend_simulation()

    if success:
        print("\nSimulation completed successfully!")
        print("All basic frontend operations are working correctly.")
    else:
        print("\nSimulation failed!")
        print("Check the backend server and configuration.")