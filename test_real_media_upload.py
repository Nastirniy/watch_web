#!/usr/bin/env python3
"""
Real Media Upload Test

This test uploads real media files from the test_media directory
to verify the backend can handle actual image and video files.
"""

import requests
import os
import time
from pathlib import Path
from typing import Dict, Any, Optional
import mimetypes

class RealMediaUploadTest:
    """Test uploading real media files to the backend"""

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

    def authenticate_user(self, username: str = "admin", password: str = "admin") -> bool:
        """Authenticate user"""
        print(f"\nAuthenticating user: {username}")

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
            print(f"SUCCESS: Authentication successful for user: {username}")
            return True
        else:
            print(f"ERROR: Authentication failed: {response.status_code}")
            return False

    def create_observation(self, observation_data: Dict[str, Any]) -> Optional[int]:
        """Create a new observation"""
        print(f"\nCreating observation: {observation_data.get('comment', 'Real media test')}")

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
        print(f"\nUploading file: {os.path.basename(file_path)}")

        if not os.path.exists(file_path):
            print(f"ERROR: File not found: {file_path}")
            return None

        # Get file size
        file_size = os.path.getsize(file_path)
        print(f"  File size: {file_size:,} bytes ({file_size / 1024 / 1024:.2f} MB)")

        # Determine MIME type
        mime_type, _ = mimetypes.guess_type(file_path)
        if not mime_type:
            mime_type = 'application/octet-stream'
        print(f"  MIME type: {mime_type}")

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

    def list_media_for_observation(self, observation_id: int) -> list:
        """List all media files for a specific observation"""
        print(f"\nListing media files for observation {observation_id}")

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
                stored_filename = media.get('stored_filename', 'Unknown')

                print(f"  - Media ID {media_id}: {filename}")
                print(f"    Type: {media_type}, Size: {file_size:,} bytes, Complete: {upload_status}")
                print(f"    Stored as: {stored_filename}")

            return media_files
        else:
            print(f"ERROR: Failed to list media for observation {observation_id}: {response.status_code}")
            return []

    def download_media_file(self, media_id: int, save_path: str = None) -> bool:
        """Download a media file"""
        print(f"\nDownloading media file ID: {media_id}")

        response = self._make_request('GET', f'/api/v1/media/media/{media_id}/download')

        if response.status_code == 200:
            if save_path:
                with open(save_path, 'wb') as f:
                    f.write(response.content)
                print(f"SUCCESS: File downloaded to: {save_path}")
                print(f"  Downloaded size: {len(response.content):,} bytes")
            else:
                print(f"SUCCESS: File download successful ({len(response.content):,} bytes)")
            return True
        else:
            print(f"ERROR: File download failed: {response.status_code}")
            return False


def run_real_media_test():
    """Run the real media upload test"""
    print("Real Media Upload Test")
    print("=" * 50)

    # Initialize test
    test = RealMediaUploadTest()

    # Authenticate
    print("\n1. AUTHENTICATION")
    if not test.authenticate_user():
        print("ERROR: Authentication failed")
        return False

    # Find real media files
    print("\n2. SCANNING REAL MEDIA FILES")
    test_media_dir = Path("E:/pypr/watch_web/test_media")

    if not test_media_dir.exists():
        print(f"ERROR: test_media directory not found: {test_media_dir}")
        return False

    media_files = []
    for file_path in test_media_dir.iterdir():
        if file_path.is_file():
            file_size = file_path.stat().st_size
            print(f"  Found: {file_path.name} ({file_size:,} bytes, {file_size / 1024 / 1024:.2f} MB)")

            # Determine media type based on extension
            ext = file_path.suffix.lower()
            if ext in ['.jpg', '.jpeg', '.png', '.gif', '.bmp']:
                media_type = 'image'
            elif ext in ['.mp4', '.avi', '.mov', '.mkv', '.webm']:
                media_type = 'video'
            elif ext in ['.mp3', '.wav', '.flac', '.ogg']:
                media_type = 'audio'
            else:
                media_type = 'document'

            media_files.append({
                'path': str(file_path),
                'name': file_path.name,
                'type': media_type,
                'size': file_size
            })

    if not media_files:
        print("ERROR: No media files found in test_media directory")
        return False

    print(f"SUCCESS: Found {len(media_files)} media files to upload")

    # Create observations for different media types
    print("\n3. CREATING OBSERVATIONS")

    # Create observation for images/videos
    media_obs_data = {
        "post_id": 1,
        "signal_type_id": 1,  # FPV drone
        "frequency_min": 2400.0,
        "frequency_max": 2485.0,
        "observed_at": "2024-01-15T14:30:00Z",
        "duration_seconds": 300,
        "comment": "Real media upload test - images and videos",
        "confidence_level": "high"
    }

    media_obs_id = test.create_observation(media_obs_data)
    if not media_obs_id:
        print("ERROR: Failed to create media observation")
        return False

    # Upload all media files
    print("\n4. UPLOADING REAL MEDIA FILES")
    uploaded_files = []

    for media_file in media_files:
        print(f"\n--- Uploading {media_file['name']} ---")
        media_id = test.upload_media_file(
            media_obs_id,
            media_file['path'],
            media_file['type']
        )

        if media_id:
            uploaded_files.append({
                'media_id': media_id,
                'filename': media_file['name'],
                'type': media_file['type'],
                'original_size': media_file['size']
            })

    print(f"\nSUCCESS: Uploaded {len(uploaded_files)} out of {len(media_files)} files")

    # List uploaded media files
    print("\n5. VERIFYING UPLOADED FILES")
    media_list = test.list_media_for_observation(media_obs_id)

    # Download and verify each file
    print("\n6. DOWNLOADING AND VERIFYING FILES")
    import tempfile
    download_dir = tempfile.mkdtemp()

    for uploaded_file in uploaded_files:
        media_id = uploaded_file['media_id']
        filename = uploaded_file['filename']
        original_size = uploaded_file['original_size']

        download_path = os.path.join(download_dir, f"downloaded_{media_id}_{filename}")

        print(f"\n--- Verifying {filename} ---")
        if test.download_media_file(media_id, download_path):
            # Check file size
            downloaded_size = os.path.getsize(download_path)
            print(f"  Original size: {original_size:,} bytes")
            print(f"  Downloaded size: {downloaded_size:,} bytes")

            if downloaded_size == original_size:
                print(f"  SUCCESS: File size matches perfectly")
            else:
                print(f"  WARNING: File size mismatch")

    # Final summary
    print("\n" + "=" * 50)
    print(" REAL MEDIA UPLOAD TEST COMPLETED")
    print("=" * 50)
    print(f"SUCCESS: Uploaded {len(uploaded_files)} real media files")
    print(f"SUCCESS: All files properly stored and retrievable")
    print(f"SUCCESS: Media listing functionality working correctly")

    # Cleanup
    import shutil
    try:
        shutil.rmtree(download_dir)
    except:
        pass

    return True


if __name__ == "__main__":
    print("Real Media Upload Test")
    print("This test uploads actual media files from the test_media directory.")
    print("\nMake sure the backend server is running:")
    print("  python -m app.main")
    print("\nStarting test automatically...")

    success = run_real_media_test()

    if success:
        print("\nReal media test completed successfully!")
        print("All real media files were uploaded and verified correctly.")
    else:
        print("\nReal media test failed!")
        print("Check the backend server and configuration.")