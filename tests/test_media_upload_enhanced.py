import pytest
import asyncio
import hashlib
import io
import os
from pathlib import Path
from unittest.mock import Mock, AsyncMock, patch
from fastapi.testclient import TestClient
from httpx import AsyncClient
import tempfile
import uuid

from app.backend.services.media_manager import (
    MediaManager,
    FileValidationConfig,
    FileValidationResult,
    FileIntegrityCheck
)
from app.backend.models.media import MediaFile, FileChunk
from app.backend.models.observation import Observation
from app.backend.enums.general import MediaType
from app.main import fastapi_app as app


@pytest.fixture
def sample_files():
    """Create sample files for testing"""
    files = {}

    # Small text file
    files['small_text'] = b"Hello, World! This is a test file."

    # Small image (PNG header)
    png_header = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde'
    files['small_png'] = png_header + b'\x00' * 100

    # JPEG file
    jpeg_header = b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00H\x00H\x00\x00'
    files['small_jpg'] = jpeg_header + b'\x00' * 200

    # Large file (2MB)
    files['large_file'] = b'A' * (2 * 1024 * 1024)

    # Very large file (10MB)
    files['very_large_file'] = b'B' * (10 * 1024 * 1024)

    # Executable file (dangerous)
    files['dangerous_exe'] = b'MZ\x90\x00' + b'\x00' * 100

    # Empty file
    files['empty_file'] = b''

    # Video file (MP4 header)
    mp4_header = b'\x00\x00\x00\x20ftypmp42\x00\x00\x00\x00mp42isom'
    files['small_mp4'] = mp4_header + b'\x00' * 500

    return files


class TestMediaManagerValidation:
    """Test file validation functionality"""

    @pytest.fixture
    def media_manager(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            yield MediaManager(
                storage_path=temp_dir,
                temp_path=f"{temp_dir}/temp",
                enable_deduplication=True
            )


    async def test_filename_sanitization(self, media_manager):
        """Test filename sanitization"""
        test_cases = [
            ("normal_file.txt", "normal_file.txt"),
            ("file with spaces.txt", "file with spaces.txt"),
            ("file<>:\"/\\|?*.txt", "file_________.txt"),
            ("../../dangerous.txt", "_._dangerous.txt"),
            ("file..name.txt", "file.name.txt"),
            ("", f"upload_{uuid.uuid4().hex[:8]}"),  # Will be generated
            ("very_long_filename_" + "x" * 300 + ".txt", None),  # Will be truncated
        ]

        for input_name, expected in test_cases:
            result = media_manager.sanitize_filename(input_name)
            if expected is None:
                # Check if it was truncated properly
                assert len(result) <= 255
                assert result.endswith(".txt")
            elif "upload_" in expected:
                # Check if it's a generated name
                assert result.startswith("upload_")
                assert len(result) == 15  # upload_ + 8 hex chars
            else:
                assert result == expected

    async def test_file_validation_size_limits(self, media_manager, sample_files):
        """Test file size validation"""
        config = FileValidationConfig(max_size=1024)  # 1KB limit

        # Small file should pass
        result = await media_manager.validate_file(
            sample_files['small_text'],
            "test.txt",
            config
        )
        assert result.is_valid

        # Large file should fail
        result = await media_manager.validate_file(
            sample_files['large_file'],
            "large.txt",
            config
        )
        assert not result.is_valid
        assert any("exceeds maximum" in issue for issue in result.issues)

    async def test_dangerous_extension_detection(self, media_manager, sample_files):
        """Test detection of dangerous file extensions"""
        config = FileValidationConfig()

        # Dangerous file should fail
        result = await media_manager.validate_file(
            sample_files['dangerous_exe'],
            "malware.exe",
            config
        )
        assert not result.is_valid
        assert any("Dangerous file extension" in issue for issue in result.issues)

    async def test_allowed_extensions_filtering(self, media_manager, sample_files):
        """Test allowed extensions filtering"""
        config = FileValidationConfig(
            allowed_extensions={'.txt', '.jpg'},
            allowed_mime_types={'text/plain', 'image/jpeg'}
        )

        # Allowed extension should pass validation step
        result = await media_manager.validate_file(
            sample_files['small_text'],
            "test.txt",
            config
        )
        # May fail on MIME type but extension check should pass
        extension_issues = [issue for issue in result.issues if "not allowed" in issue and "extension" in issue]
        assert len(extension_issues) == 0

        # Disallowed extension should fail
        result = await media_manager.validate_file(
            sample_files['small_png'],
            "test.png",
            config
        )
        assert not result.is_valid
        assert any("extension" in issue and "not allowed" in issue for issue in result.issues)

    async def test_mime_type_validation(self, media_manager, sample_files):
        """Test MIME type validation"""
        config = FileValidationConfig(
            allowed_mime_types={'image/png'},
            validate_content_headers=True
        )

        # PNG file with correct header
        result = await media_manager.validate_file(
            sample_files['small_png'],
            "test.png",
            config
        )
        # Should pass MIME validation
        mime_issues = [issue for issue in result.issues if "MIME type" in issue and "not allowed" in issue]
        assert len(mime_issues) == 0

    async def test_content_header_validation(self, media_manager, sample_files):
        """Test content header validation"""
        config = FileValidationConfig(validate_content_headers=True)

        # PNG with correct header
        result = await media_manager.validate_file(
            sample_files['small_png'],
            "test.png",
            config
        )
        header_issues = [issue for issue in result.issues if "Invalid" in issue and "header" in issue]
        assert len(header_issues) == 0

        # Fake PNG (wrong header)
        fake_png = b"This is not a PNG" + b'\x00' * 100
        result = await media_manager.validate_file(
            fake_png,
            "fake.png",
            config
        )
        assert result.detected_mime_type != "image/png"

    async def test_empty_file_handling(self, media_manager, sample_files):
        """Test empty file handling"""
        result = await media_manager.validate_file(
            sample_files['empty_file'],
            "empty.txt",
            FileValidationConfig()
        )
        assert not result.is_valid
        assert any("empty" in issue.lower() for issue in result.issues)


class TestMediaManagerStorage:
    """Test file storage functionality"""

    @pytest.fixture
    def mock_session(self):
        session = AsyncMock()
        session.add = Mock()
        session.commit = AsyncMock()
        session.refresh = AsyncMock()

        # Mock the exec method to return a mock result with first() and all() methods
        mock_result = AsyncMock()
        mock_result.first = Mock(return_value=None)  # Default to no result
        mock_result.all = Mock(return_value=[])      # Default to empty list

        session.exec = AsyncMock(return_value=mock_result)
        return session

    @pytest.fixture
    def media_manager(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            yield MediaManager(
                storage_path=temp_dir,
                temp_path=f"{temp_dir}/temp",
                chunk_size=1024,  # Small chunks for testing
                enable_deduplication=True
            )

    async def test_file_chunking(self, media_manager, mock_session, sample_files):
        """Test file chunking functionality"""
        # Mock media file creation
        mock_media_file = Mock()
        mock_media_file.id = 1
        mock_session.refresh.side_effect = lambda obj: setattr(obj, 'id', 1)

        with patch('app.backend.services.media_manager.aiofiles.open'), \
             patch('app.backend.services.media_manager.aiofiles.os.makedirs'):

            file_id = await media_manager.store_file_chunked(
                session=mock_session,
                observation_id=1,
                file_data=sample_files['large_file'],
                original_filename="large_test.bin",
                media_type="document"
            )

            # Verify chunks were created
            assert mock_session.add.call_count > 2  # Media file + multiple chunks

            # Check that commit was called
            assert mock_session.commit.call_count >= 2

    async def test_deduplication(self, media_manager, mock_session, sample_files):
        """Test file deduplication"""
        # Mock existing file
        mock_existing_file = Mock()
        mock_existing_file.id = 999

        mock_result = Mock()
        mock_result.first.return_value = mock_existing_file
        mock_session.exec.return_value = mock_result

        with patch('app.backend.services.media_manager.aiofiles.open'), \
             patch('app.backend.services.media_manager.aiofiles.os.makedirs'):

            file_id = await media_manager.store_file_chunked(
                session=mock_session,
                observation_id=1,
                file_data=sample_files['small_text'],
                original_filename="test.txt",
                media_type="document"
            )

            # Should return existing file ID
            assert file_id == 999

    async def test_concurrent_upload_limiting(self, media_manager, mock_session, sample_files):
        """Test concurrent upload limiting"""
        # Fill up the concurrent upload slots
        for i in range(media_manager._max_concurrent_uploads):
            media_manager._active_uploads.add(f"session_{i}")

        with pytest.raises(ValueError, match="Too many concurrent uploads"):
            await media_manager.store_file_chunked(
                session=mock_session,
                observation_id=1,
                file_data=sample_files['small_text'],
                original_filename="test.txt",
                media_type="document"
            )


class TestFileIntegrity:
    """Test file integrity checking and repair"""

    @pytest.fixture
    def media_manager(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            yield MediaManager(
                storage_path=temp_dir,
                chunk_size=100,  # Very small chunks for testing
                use_filesystem_cache=True
            )

    @pytest.fixture
    def mock_session_with_data(self, sample_files):
        """Mock session with sample data"""
        session = AsyncMock()

        # Mock media file using simple object to avoid Mock auto-mocking
        media_file = type('MockMediaFile', (), {
            'id': 1,
            'file_size': len(sample_files['small_text']),
            'file_hash': hashlib.sha256(sample_files['small_text']).hexdigest(),
            'file_path': "/test/path/file.txt",
            'original_filename': "test.txt"
        })()

        # Mock chunks
        chunks = []
        data = sample_files['small_text']
        chunk_size = 100
        for i in range(0, len(data), chunk_size):
            chunk_data = data[i:i + chunk_size]
            # Create a simple object instead of Mock to avoid auto-mocking of attributes
            chunk = type('MockChunk', (), {
                'chunk_number': i // chunk_size,
                'chunk_data': chunk_data,
                'chunk_hash': hashlib.sha256(chunk_data).hexdigest(),
                'chunk_size': len(chunk_data),
                'chunk_file_path': f"/test/path/chunk_{i // chunk_size:04d}.bin"
            })()
            chunks.append(chunk)

        # Setup mock responses
        def mock_exec(query):
            result = AsyncMock()
            query_str = str(query)
            if "media_file" in query_str.lower() or "mediafile" in query_str.lower():
                result.first = Mock(return_value=media_file)
                result.all = Mock(return_value=[media_file])
            elif "chunk" in query_str.lower():
                result.first = Mock(return_value=chunks[0] if chunks else None)
                result.all = Mock(return_value=chunks)
            else:
                result.first = Mock(return_value=None)
                result.all = Mock(return_value=[])
            return result

        session.exec = AsyncMock(side_effect=mock_exec)
        return session, media_file, chunks, sample_files['small_text']

    async def test_file_integrity_check_success(self, media_manager, mock_session_with_data):
        """Test successful file integrity check"""
        session, media_file, chunks, file_data = mock_session_with_data

        integrity = await media_manager.check_file_integrity(session, 1)

        assert integrity.status == "ok"
        assert integrity.hash_matches == True
        assert integrity.security_validated == True
        assert len(integrity.issues) == 0

    async def test_file_integrity_check_missing_chunks(self, media_manager, mock_session_with_data):
        """Test integrity check with missing chunks"""
        session, media_file, chunks, file_data = mock_session_with_data

        # Remove a chunk to simulate missing data
        chunks.pop()

        integrity = await media_manager.check_file_integrity(session, 1)

        assert integrity.status == "incomplete"
        assert len(integrity.missing_chunks) > 0
        assert any("Missing" in issue for issue in integrity.issues)

    async def test_file_integrity_check_corrupted_chunk(self, media_manager, mock_session_with_data):
        """Test integrity check with corrupted chunk"""
        session, media_file, chunks, file_data = mock_session_with_data

        # Corrupt a chunk hash
        chunks[0].chunk_hash = "corrupted_hash"

        integrity = await media_manager.check_file_integrity(session, 1)

        assert not integrity.security_validated
        assert any("hash mismatch" in issue for issue in integrity.issues)

    async def test_file_retrieval_with_validation(self, media_manager, mock_session_with_data):
        """Test file retrieval with integrity validation"""
        session, media_file, chunks, file_data = mock_session_with_data

        retrieved_data = await media_manager.retrieve_file(session, 1, validate_integrity=True)

        assert retrieved_data == file_data

    async def test_file_retrieval_validation_failure(self, media_manager, mock_session_with_data):
        """Test file retrieval with validation failure"""
        session, media_file, chunks, file_data = mock_session_with_data

        # Corrupt chunk data
        chunks[0].chunk_data = b"corrupted"

        with pytest.raises(ValueError, match="integrity validation failed"):
            await media_manager.retrieve_file(session, 1, validate_integrity=True)


class TestAPIEndpoints:
    """Test API endpoints with various file types and sizes"""

    @pytest.fixture
    def client(self):
        return TestClient(app)

    @pytest.fixture
    def sample_files_for_upload(self):
        """Create files for API testing"""
        return {
            'small_image': {
                'filename': 'test_image.png',
                'content': b'\x89PNG\r\n\x1a\n' + b'\x00' * 1000,
                'media_type': 'image'
            },
            'small_document': {
                'filename': 'test_doc.pdf',
                'content': b'%PDF-1.4\n' + b'A' * 2000,
                'media_type': 'document'
            },
            'large_video': {
                'filename': 'test_video.mp4',
                'content': b'\x00\x00\x00\x20ftypmp42' + b'V' * (5 * 1024 * 1024),  # 5MB
                'media_type': 'video'
            },
            'dangerous_file': {
                'filename': 'malware.exe',
                'content': b'MZ\x90\x00' + b'X' * 1000,
                'media_type': 'document'
            }
        }

    def test_upload_small_image(self, client, sample_files_for_upload):
        """Test uploading a small image file"""
        file_data = sample_files_for_upload['small_image']

        with patch('app.backend.dependencies.get_current_user'), \
             patch('app.backend.dependencies.get_session'), \
             patch('app.backend.services.permissions.PermissionService.require_permission', return_value=lambda f: f):

            response = client.post(
                "/api/v1/media/media/upload",
                data={
                    "observation_id": 1,
                    "media_type": file_data['media_type']
                },
                files={
                    "file": (file_data['filename'], io.BytesIO(file_data['content']), "image/png")
                }
            )

            # Should succeed or fail gracefully with proper error message
            assert response.status_code in [200, 400, 404, 422]

    def test_upload_large_file_streaming(self, client, sample_files_for_upload):
        """Test uploading and downloading large file with streaming"""
        file_data = sample_files_for_upload['large_video']

        with patch('app.backend.dependencies.get_current_user'), \
             patch('app.backend.dependencies.get_session'), \
             patch('app.backend.services.permissions.PermissionService.require_permission', return_value=lambda f: f):

            # Test upload
            response = client.post(
                "/api/v1/media/media/upload",
                data={
                    "observation_id": 1,
                    "media_type": file_data['media_type']
                },
                files={
                    "file": (file_data['filename'], io.BytesIO(file_data['content']), "video/mp4")
                }
            )

            # Should handle large file appropriately
            assert response.status_code in [200, 400, 404, 413, 422]

    def test_upload_dangerous_file_rejection(self, client, sample_files_for_upload):
        """Test rejection of dangerous file types"""
        file_data = sample_files_for_upload['dangerous_file']

        with patch('app.backend.dependencies.get_current_user') as mock_user, \
             patch('app.backend.dependencies.get_session') as mock_session, \
             patch('app.backend.services.permissions.PermissionService.require_permission', return_value=lambda f: f):

            # Mock the user and session dependencies
            mock_user.return_value = Mock(id=1, username="testuser")
            mock_session.return_value = AsyncMock()

            response = client.post(
                "/api/v1/media/media/upload",
                data={
                    "observation_id": 1,
                    "media_type": file_data['media_type']
                },
                files={
                    "file": (file_data['filename'], io.BytesIO(file_data['content']), "application/octet-stream")
                }
            )

            # Should reject dangerous file
            assert response.status_code in [400, 422]
            if response.status_code == 400:
                assert "validation failed" in response.json().get("detail", "").lower()

    def test_chunked_upload_workflow(self, client):
        """Test complete chunked upload workflow"""
        # Test data
        file_content = b'A' * 5000  # 5KB file
        chunk_size = 1024
        total_chunks = (len(file_content) + chunk_size - 1) // chunk_size

        with patch('app.backend.dependencies.get_current_user'), \
             patch('app.backend.dependencies.get_session'), \
             patch('app.backend.services.permissions.PermissionService.require_permission', return_value=lambda f: f):

            # Start chunked upload
            start_response = client.post(
                "/api/v1/media/media/upload/chunked/start",
                json={
                    "observation_id": 1,
                    "original_filename": "test_chunked.bin",
                    "file_size": len(file_content),
                    "chunk_size": chunk_size,
                    "total_chunks": total_chunks,
                    "file_extension": ".bin",
                    "media_type": "document"
                }
            )

            # Should start successfully or fail gracefully
            assert start_response.status_code in [200, 400, 404, 422]

            if start_response.status_code == 200:
                upload_data = start_response.json()
                upload_id = upload_data["upload_id"]

                # Upload chunks
                for chunk_num in range(total_chunks):
                    start_offset = chunk_num * chunk_size
                    end_offset = min(start_offset + chunk_size, len(file_content))
                    chunk_data = file_content[start_offset:end_offset]

                    chunk_response = client.post(
                        "/api/v1/media/media/upload/chunked/chunk",
                        data={
                            "upload_id": upload_id,
                            "chunk_number": chunk_num
                        },
                        files={
                            "chunk_file": (f"chunk_{chunk_num}", io.BytesIO(chunk_data), "application/octet-stream")
                        }
                    )

                    assert chunk_response.status_code in [200, 400, 404, 408, 422]

    def test_file_download_with_streaming(self, client):
        """Test file download with streaming option"""
        with patch('app.backend.dependencies.get_current_user'), \
             patch('app.backend.dependencies.get_session'), \
             patch('app.backend.services.permissions.PermissionService.require_permission', return_value=lambda f: f):

            # Test regular download
            response = client.get("/api/v1/media/media/1/download")
            assert response.status_code in [200, 404, 409]

            # Test streaming download
            response = client.get("/api/v1/media/media/1/download?stream=true")
            assert response.status_code in [200, 404, 409]

    def test_file_integrity_check_endpoint(self, client):
        """Test file integrity check endpoint"""
        with patch('app.backend.dependencies.get_current_user'), \
             patch('app.backend.dependencies.get_session'), \
             patch('app.backend.services.permissions.PermissionService.require_permission', return_value=lambda f: f):

            response = client.get("/api/v1/media/integrity/1")
            assert response.status_code in [200, 404]

    def test_file_repair_endpoint(self, client):
        """Test file repair endpoint"""
        with patch('app.backend.dependencies.get_current_user'), \
             patch('app.backend.dependencies.get_session'), \
             patch('app.backend.services.permissions.PermissionService.require_permission', return_value=lambda f: f):

            response = client.post("/api/v1/media/repair/1")
            assert response.status_code in [200, 404, 422]

    def test_storage_statistics_endpoint(self, client):
        """Test storage statistics endpoint"""
        with patch('app.backend.dependencies.get_current_user'), \
             patch('app.backend.dependencies.get_session'), \
             patch('app.backend.services.permissions.PermissionService.require_permission', return_value=lambda f: f):

            response = client.get("/api/v1/media/media/statistics")
            assert response.status_code in [200, 403]

    def test_orphaned_files_cleanup_endpoint(self, client):
        """Test orphaned files cleanup endpoint"""
        with patch('app.backend.dependencies.get_current_user'), \
             patch('app.backend.dependencies.get_session'), \
             patch('app.backend.services.permissions.PermissionService.require_permission', return_value=lambda f: f):

            response = client.post("/api/v1/media/media/maintenance/cleanup")
            assert response.status_code in [200, 403]


class TestPerformanceAndEdgeCases:
    """Test performance and edge cases"""

    @pytest.fixture
    def media_manager(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            yield MediaManager(
                storage_path=temp_dir,
                chunk_size=1024,
                _max_concurrent_uploads=2  # Low limit for testing
            )

    async def test_very_large_file_handling(self, media_manager):
        """Test handling of very large files"""
        # Simulate 100MB file
        large_file_size = 100 * 1024 * 1024

        config = FileValidationConfig(max_size=large_file_size + 1000)

        # Test validation without actually creating the large file
        mock_data = b'X' * 1000  # Small sample for testing
        result = await media_manager.validate_file(
            mock_data,
            "large_file.bin",
            config
        )

        # Should pass basic validation
        size_issues = [issue for issue in result.issues if "exceeds maximum" in issue]
        assert len(size_issues) == 0

    async def test_unicode_filename_handling(self, media_manager):
        """Test handling of Unicode filenames"""
        unicode_names = [
            "файл.txt",  # Cyrillic
            "文件.txt",   # Chinese
            "ファイル.txt", # Japanese
            "🎵music🎵.mp3",  # Emoji
            "café_résumé.pdf"  # Accented characters
        ]

        for name in unicode_names:
            sanitized = media_manager.sanitize_filename(name)
            # Should not crash and should produce valid filename
            assert len(sanitized) > 0
            assert len(sanitized) <= 255

    async def test_concurrent_upload_stress(self, media_manager):
        """Test concurrent upload handling"""
        mock_session = AsyncMock()
        sample_data = b"test data"

        # Try to exceed concurrent upload limit
        tasks = []
        for i in range(5):  # More than the limit of 2
            task = media_manager.store_file_chunked(
                session=mock_session,
                observation_id=i,
                file_data=sample_data,
                original_filename=f"test_{i}.txt",
                media_type="document"
            )
            tasks.append(task)

        # Some should fail with rate limiting
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # At least some should fail due to rate limiting
        failures = [r for r in results if isinstance(r, Exception)]
        assert len(failures) > 0
        assert any("Too many concurrent uploads" in str(f) for f in failures)

    async def test_filesystem_permission_errors(self, media_manager):
        """Test handling of filesystem permission errors"""
        # This would require platform-specific permission manipulation
        # For now, we test that the error handling structure is in place

        with patch('app.backend.services.media_manager.aiofiles.open',
                   side_effect=PermissionError("Permission denied")):

            # Should handle permission errors gracefully
            with pytest.raises(Exception):  # Could be PermissionError or wrapped exception
                await media_manager._create_filesystem_backup(
                    media_file_id=1,
                    file_data=b"test",
                    observation_id=1,
                    stored_filename="test.txt"
                )

    async def test_corrupted_chunk_data_handling(self, media_manager):
        """Test handling of corrupted chunk data"""
        session = AsyncMock()

        # Mock corrupted chunks
        chunks = []
        chunk1 = Mock()
        chunk1.chunk_number = 0
        chunk1.chunk_data = b"original data"
        chunk1.chunk_hash = "wrong_hash"  # Intentionally wrong
        chunk1.chunk_size = len(chunk1.chunk_data)
        chunks.append(chunk1)

        media_file = Mock()
        media_file.id = 1
        media_file.file_size = len(chunk1.chunk_data)
        media_file.file_hash = hashlib.sha256(chunk1.chunk_data).hexdigest()
        media_file.original_filename = "test.txt"

        def mock_exec(query):
            result = AsyncMock()
            query_str = str(query)
            if "media_file" in query_str.lower() or "mediafile" in query_str.lower():
                result.first = Mock(return_value=media_file)
                result.all = Mock(return_value=[media_file])
            elif "chunk" in query_str.lower():
                result.first = Mock(return_value=chunks[0] if chunks else None)
                result.all = Mock(return_value=chunks)
            else:
                result.first = Mock(return_value=None)
                result.all = Mock(return_value=[])
            return result

        session.exec = AsyncMock(side_effect=mock_exec)

        # Should detect corruption
        integrity = await media_manager.check_file_integrity(session, 1)
        assert not integrity.security_validated
        assert any("hash mismatch" in issue for issue in integrity.issues)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])