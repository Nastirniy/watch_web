import pytest
import asyncio
import tempfile
import hashlib
import io
from pathlib import Path
from unittest.mock import Mock, AsyncMock, patch
import uuid

from app.backend.services.media_manager import MediaManager, FileValidationConfig
from app.backend.models.media import MediaFile, FileChunk
from app.backend.enums.general import MediaType


class TestRealFileOperations:
    """Integration tests with real file operations"""

    @pytest.fixture
    async def setup_media_manager(self):
        """Setup media manager with real temporary directories"""
        with tempfile.TemporaryDirectory() as temp_dir:
            storage_path = Path(temp_dir) / "storage"
            temp_path = Path(temp_dir) / "temp"

            manager = MediaManager(
                storage_path=str(storage_path),
                temp_path=str(temp_path),
                chunk_size=1024,  # Small chunks for testing
                use_filesystem_cache=True,
                enable_deduplication=True
            )

            yield manager, temp_dir

    @pytest.fixture
    def real_test_files(self):
        """Create real test files with various characteristics"""
        files = {}

        # Small text file
        files['text_small'] = {
            'content': b"Hello World! This is a small test file for validation.",
            'filename': 'small_text.txt',
            'media_type': 'document',
            'expected_mime': 'text/plain'
        }

        # Medium binary file
        files['binary_medium'] = {
            'content': bytes(range(256)) * 100,  # 25.6KB of binary data
            'filename': 'medium_binary.bin',
            'media_type': 'document',
            'expected_mime': 'application/octet-stream'
        }

        # Large file (2MB)
        files['large_data'] = {
            'content': b'LARGE_FILE_DATA_' * (2 * 1024 * 1024 // 16),  # ~2MB
            'filename': 'large_data.dat',
            'media_type': 'document',
            'expected_mime': 'application/octet-stream'
        }

        # Fake PNG (PNG header but not real PNG)
        png_header = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde'
        files['fake_png'] = {
            'content': png_header + b'\x00' * 200,
            'filename': 'test_image.png',
            'media_type': 'image',
            'expected_mime': 'image/png'
        }

        # Real JPEG header
        jpeg_header = b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00H\x00H\x00\x00\xff\xfe\x00\x13Created with GIMP\xff\xdb'
        files['fake_jpeg'] = {
            'content': jpeg_header + b'\x00' * 500,
            'filename': 'test_photo.jpg',
            'media_type': 'image',
            'expected_mime': 'image/jpeg'
        }

        # MP4 video header
        mp4_header = b'\x00\x00\x00\x20ftypmp42\x00\x00\x00\x00mp42isom\x00\x00\x00\x08free'
        files['fake_mp4'] = {
            'content': mp4_header + b'V' * 1000,
            'filename': 'test_video.mp4',
            'media_type': 'video',
            'expected_mime': 'video/mp4'
        }

        return files

    async def test_complete_file_lifecycle(self, setup_media_manager, real_test_files):
        """Test complete file upload, storage, retrieval, and cleanup lifecycle"""
        manager, temp_dir = setup_media_manager
        test_file = real_test_files['text_small']

        # Mock database session
        session = AsyncMock()
        session.add = Mock()
        session.commit = AsyncMock()
        session.refresh = AsyncMock()

        # Mock media file ID assignment
        def mock_refresh(obj):
            if hasattr(obj, 'id') and obj.id is None:
                obj.id = 1
        session.refresh.side_effect = mock_refresh

        # Store file
        file_id = await manager.store_file_chunked(
            session=session,
            observation_id=1,
            file_data=test_file['content'],
            original_filename=test_file['filename'],
            media_type=test_file['media_type']
        )

        assert file_id == 1
        assert session.add.call_count >= 2  # MediaFile + at least one chunk

        # Verify filesystem backup was created
        storage_files = list(Path(temp_dir).rglob("*.*"))
        assert len(storage_files) > 0, "No files created in storage"

    async def test_chunking_integrity_with_large_file(self, setup_media_manager, real_test_files):
        """Test that large files are chunked correctly and can be reassembled"""
        manager, temp_dir = setup_media_manager
        test_file = real_test_files['large_data']

        session = AsyncMock()
        session.add = Mock()
        session.commit = AsyncMock()
        session.refresh = AsyncMock()

        # Track chunks that get created
        created_chunks = []

        def mock_add(obj):
            if isinstance(obj, FileChunk):
                created_chunks.append(obj)

        session.add.side_effect = mock_add

        def mock_refresh(obj):
            if hasattr(obj, 'id') and obj.id is None:
                obj.id = 1
        session.refresh.side_effect = mock_refresh

        # Store file
        await manager.store_file_chunked(
            session=session,
            observation_id=1,
            file_data=test_file['content'],
            original_filename=test_file['filename'],
            media_type=test_file['media_type']
        )

        # Verify chunks
        expected_chunks = (len(test_file['content']) + manager.chunk_size - 1) // manager.chunk_size
        actual_chunks = len([obj for obj in session.add.call_args_list if isinstance(obj[0][0], FileChunk)])

        assert actual_chunks == expected_chunks, f"Expected {expected_chunks} chunks, got {actual_chunks}"

        # Verify chunk data integrity
        total_chunk_size = sum(len(call[0][0].chunk_data) for call in session.add.call_args_list
                              if isinstance(call[0][0], FileChunk))
        assert total_chunk_size == len(test_file['content']), "Chunk data size mismatch"

    async def test_file_validation_with_real_headers(self, setup_media_manager, real_test_files):
        """Test validation with real file headers"""
        manager, _ = setup_media_manager

        # Test PNG validation
        png_file = real_test_files['fake_png']
        config = FileValidationConfig(
            allowed_extensions={'.png'},
            allowed_mime_types={'image/png'},
            validate_content_headers=True
        )

        result = await manager.validate_file(
            png_file['content'],
            png_file['filename'],
            config
        )

        # Should pass header validation
        header_issues = [issue for issue in result.issues if "Invalid" in issue and "header" in issue]
        assert len(header_issues) == 0, f"PNG header validation failed: {header_issues}"

        # Test JPEG validation
        jpeg_file = real_test_files['fake_jpeg']
        config = FileValidationConfig(
            allowed_extensions={'.jpg'},
            allowed_mime_types={'image/jpeg'},
            validate_content_headers=True
        )

        result = await manager.validate_file(
            jpeg_file['content'],
            jpeg_file['filename'],
            config
        )

        header_issues = [issue for issue in result.issues if "Invalid" in issue and "header" in issue]
        assert len(header_issues) == 0, f"JPEG header validation failed: {header_issues}"

    async def test_deduplication_with_real_files(self, setup_media_manager, real_test_files):
        """Test deduplication with real file data"""
        manager, temp_dir = setup_media_manager
        test_file = real_test_files['binary_medium']

        session = AsyncMock()
        session.add = Mock()
        session.commit = AsyncMock()
        session.refresh = AsyncMock()

        # Mock database responses for deduplication check
        call_count = 0
        def mock_exec(query):
            nonlocal call_count
            call_count += 1
            result = Mock()

            if call_count <= 2:  # First upload - no duplicates
                result.first.return_value = None
            else:  # Second upload - duplicate found
                mock_duplicate = Mock()
                mock_duplicate.id = 1
                result.first.return_value = mock_duplicate

            return result

        session.exec = AsyncMock(side_effect=mock_exec)

        def mock_refresh(obj):
            if hasattr(obj, 'id') and obj.id is None:
                obj.id = 1
        session.refresh.side_effect = mock_refresh

        # First upload
        file_id_1 = await manager.store_file_chunked(
            session=session,
            observation_id=1,
            file_data=test_file['content'],
            original_filename=test_file['filename'],
            media_type=test_file['media_type']
        )

        # Second upload (same file)
        file_id_2 = await manager.store_file_chunked(
            session=session,
            observation_id=1,
            file_data=test_file['content'],
            original_filename=test_file['filename'],
            media_type=test_file['media_type']
        )

        assert file_id_1 == file_id_2, "Deduplication should return same file ID"

    async def test_filesystem_backup_and_retrieval(self, setup_media_manager, real_test_files):
        """Test filesystem backup creation and file retrieval"""
        manager, temp_dir = setup_media_manager
        test_file = real_test_files['medium_binary']

        # Create a real filesystem backup
        observation_id = 42
        stored_filename = "test_backup.bin"

        await manager._create_filesystem_backup(
            media_file_id=1,
            file_data=test_file['content'],
            observation_id=observation_id,
            stored_filename=stored_filename
        )

        # Verify file was created
        expected_path = manager.storage_path / str(observation_id) / stored_filename
        assert expected_path.exists(), f"Backup file not created at {expected_path}"

        # Verify file content
        with open(expected_path, 'rb') as f:
            stored_content = f.read()

        assert stored_content == test_file['content'], "Stored content doesn't match original"

        # Test retrieval
        mock_media_file = Mock()
        mock_media_file.id = 1
        mock_media_file.file_path = str(expected_path)
        mock_media_file.file_hash = hashlib.sha256(test_file['content']).hexdigest()

        retrieved_data = await manager._retrieve_from_filesystem(mock_media_file, validate_integrity=True)

        assert retrieved_data == test_file['content'], "Retrieved content doesn't match original"

    async def test_concurrent_file_operations(self, setup_media_manager, real_test_files):
        """Test concurrent file operations"""
        manager, temp_dir = setup_media_manager

        # Prepare multiple files
        files_to_upload = [
            real_test_files['text_small'],
            real_test_files['binary_medium'],
            real_test_files['fake_png']
        ]

        # Mock session for each upload
        async def create_mock_session():
            session = AsyncMock()
            session.add = Mock()
            session.commit = AsyncMock()
            session.refresh = AsyncMock()

            upload_count = 0
            def mock_refresh(obj):
                nonlocal upload_count
                if hasattr(obj, 'id') and obj.id is None:
                    upload_count += 1
                    obj.id = upload_count
            session.refresh.side_effect = mock_refresh

            def mock_exec(query):
                result = Mock()
                result.first.return_value = None  # No duplicates
                return result
            session.exec = AsyncMock(side_effect=mock_exec)

            return session

        # Create upload tasks
        tasks = []
        for i, file_data in enumerate(files_to_upload):
            session = await create_mock_session()
            task = manager.store_file_chunked(
                session=session,
                observation_id=i + 1,
                file_data=file_data['content'],
                original_filename=file_data['filename'],
                media_type=file_data['media_type']
            )
            tasks.append(task)

        # Execute concurrently
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Check results
        successful_uploads = [r for r in results if not isinstance(r, Exception)]
        failed_uploads = [r for r in results if isinstance(r, Exception)]

        # Should have some successful uploads (may hit rate limit)
        assert len(successful_uploads) > 0, f"No successful uploads. Failures: {failed_uploads}"

    async def test_file_integrity_with_real_corruption(self, setup_media_manager, real_test_files):
        """Test integrity checking with real file corruption scenarios"""
        manager, temp_dir = setup_media_manager
        test_file = real_test_files['large_data']

        # Create mock file and chunks with real data
        media_file = Mock()
        media_file.id = 1
        media_file.file_size = len(test_file['content'])
        media_file.file_hash = hashlib.sha256(test_file['content']).hexdigest()
        media_file.original_filename = test_file['filename']
        media_file.file_path = str(manager.storage_path / "1" / test_file['filename'])

        # Create chunks from real data
        chunks = []
        chunk_size = manager.chunk_size
        for i in range(0, len(test_file['content']), chunk_size):
            chunk_data = test_file['content'][i:i + chunk_size]
            chunk = Mock()
            chunk.chunk_number = i // chunk_size
            chunk.chunk_data = chunk_data
            chunk.chunk_hash = hashlib.sha256(chunk_data).hexdigest()
            chunk.chunk_size = len(chunk_data)
            chunks.append(chunk)

        # Test 1: All chunks intact
        session = AsyncMock()
        def mock_exec(query):
            result = Mock()
            if "MediaFile" in str(query):
                result.first.return_value = media_file
            elif "FileChunk" in str(query):
                result.all.return_value = chunks
            return result
        session.exec = AsyncMock(side_effect=mock_exec)

        integrity = await manager.check_file_integrity(session, 1)
        assert integrity.status == "ok"
        assert integrity.hash_matches == True

        # Test 2: Missing chunk
        incomplete_chunks = chunks[:-1]  # Remove last chunk
        def mock_exec_incomplete(query):
            result = Mock()
            if "MediaFile" in str(query):
                result.first.return_value = media_file
            elif "FileChunk" in str(query):
                result.all.return_value = incomplete_chunks
            return result
        session.exec = AsyncMock(side_effect=mock_exec_incomplete)

        integrity = await manager.check_file_integrity(session, 1)
        assert integrity.status == "incomplete"
        assert len(integrity.missing_chunks) > 0

        # Test 3: Corrupted chunk
        corrupted_chunks = chunks.copy()
        corrupted_chunks[0].chunk_data = b"CORRUPTED" + corrupted_chunks[0].chunk_data[9:]  # Change some data
        def mock_exec_corrupted(query):
            result = Mock()
            if "MediaFile" in str(query):
                result.first.return_value = media_file
            elif "FileChunk" in str(query):
                result.all.return_value = corrupted_chunks
            return result
        session.exec = AsyncMock(side_effect=mock_exec_corrupted)

        integrity = await manager.check_file_integrity(session, 1)
        assert not integrity.security_validated
        assert any("hash mismatch" in issue for issue in integrity.issues)

    async def test_storage_statistics_with_real_data(self, setup_media_manager, real_test_files):
        """Test storage statistics with real file data"""
        manager, temp_dir = setup_media_manager

        # Mock database with realistic data
        mock_files = []
        mock_chunks = []

        for i, (file_key, file_data) in enumerate(real_test_files.items()):
            # Create mock media file
            media_file = Mock()
            media_file.id = i + 1
            media_file.media_type = file_data['media_type']
            media_file.file_size = len(file_data['content'])
            mock_files.append(media_file)

            # Create mock chunks
            chunk_size = manager.chunk_size
            for j in range(0, len(file_data['content']), chunk_size):
                chunk_data = file_data['content'][j:j + chunk_size]
                chunk = Mock()
                chunk.chunk_size = len(chunk_data)
                mock_chunks.append(chunk)

        session = AsyncMock()
        def mock_exec(query):
            result = Mock()
            if "MediaFile" in str(query):
                result.all.return_value = mock_files
            elif "FileChunk" in str(query):
                result.all.return_value = mock_chunks
            return result
        session.exec = AsyncMock(side_effect=mock_exec)

        # Get statistics
        stats = await manager.get_storage_statistics(session)

        # Verify statistics
        assert stats['total_files'] == len(mock_files)
        assert stats['total_chunks'] == len(mock_chunks)
        assert stats['total_database_size_bytes'] > 0
        assert 'media_types' in stats
        assert 'chunk_size' in stats


class TestStressAndPerformance:
    """Stress tests and performance validation"""

    @pytest.fixture
    async def stress_media_manager(self):
        """Setup for stress testing"""
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = MediaManager(
                storage_path=temp_dir,
                chunk_size=1024,
                _max_concurrent_uploads=5  # Limited for testing
            )
            yield manager

    async def test_many_small_files(self, stress_media_manager):
        """Test handling many small files"""
        manager = stress_media_manager

        # Create many small files
        num_files = 50
        file_size = 100  # 100 bytes each

        sessions = []
        tasks = []

        for i in range(num_files):
            session = AsyncMock()
            session.add = Mock()
            session.commit = AsyncMock()
            session.refresh = AsyncMock()

            # Mock unique file ID
            def make_refresh(file_id):
                def mock_refresh(obj):
                    if hasattr(obj, 'id') and obj.id is None:
                        obj.id = file_id
                return mock_refresh
            session.refresh.side_effect = make_refresh(i + 1)

            # Mock no duplicates
            def mock_exec(query):
                result = Mock()
                result.first.return_value = None
                return result
            session.exec = AsyncMock(side_effect=mock_exec)

            sessions.append(session)

            # Create task
            file_data = f"File {i} content: " + "x" * file_size
            task = manager.store_file_chunked(
                session=session,
                observation_id=1,
                file_data=file_data.encode(),
                original_filename=f"file_{i:03d}.txt",
                media_type="document"
            )
            tasks.append(task)

        # Execute with rate limiting
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Analyze results
        successful = [r for r in results if not isinstance(r, Exception)]
        failed = [r for r in results if isinstance(r, Exception)]

        print(f"Successful uploads: {len(successful)}")
        print(f"Failed uploads: {len(failed)}")

        # Should handle rate limiting gracefully
        if failed:
            rate_limit_errors = [e for e in failed if "Too many concurrent uploads" in str(e)]
            assert len(rate_limit_errors) > 0, "Expected some rate limiting"

    async def test_file_size_boundaries(self, stress_media_manager):
        """Test various file size boundaries"""
        manager = stress_media_manager

        # Test different file sizes around chunk boundaries
        test_sizes = [
            0,  # Empty
            1,  # 1 byte
            511,  # Just under 512
            512,  # Exactly 512
            513,  # Just over 512
            1023,  # Just under 1KB (chunk size)
            1024,  # Exactly 1KB
            1025,  # Just over 1KB
            2048,  # 2KB
            1024 * 1024,  # 1MB
            1024 * 1024 + 1,  # Just over 1MB
        ]

        for size in test_sizes:
            if size == 0:
                file_data = b""
            else:
                file_data = b"X" * size

            session = AsyncMock()
            session.add = Mock()
            session.commit = AsyncMock()
            session.refresh = AsyncMock()

            def mock_refresh(obj):
                if hasattr(obj, 'id') and obj.id is None:
                    obj.id = 1
            session.refresh.side_effect = mock_refresh

            def mock_exec(query):
                result = Mock()
                result.first.return_value = None
                return result
            session.exec = AsyncMock(side_effect=mock_exec)

            try:
                if size == 0:
                    # Empty files should fail validation
                    with pytest.raises(ValueError, match="validation failed"):
                        await manager.store_file_chunked(
                            session=session,
                            observation_id=1,
                            file_data=file_data,
                            original_filename=f"size_{size}.bin",
                            media_type="document"
                        )
                else:
                    # Non-empty files should succeed
                    file_id = await manager.store_file_chunked(
                        session=session,
                        observation_id=1,
                        file_data=file_data,
                        original_filename=f"size_{size}.bin",
                        media_type="document"
                    )
                    assert file_id == 1

                    # Verify correct number of chunks
                    expected_chunks = max(1, (size + manager.chunk_size - 1) // manager.chunk_size)
                    chunk_adds = len([call for call in session.add.call_args_list
                                    if isinstance(call[0][0], FileChunk)])
                    assert chunk_adds == expected_chunks, f"Size {size}: expected {expected_chunks} chunks, got {chunk_adds}"

            except Exception as e:
                pytest.fail(f"Unexpected error for size {size}: {e}")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])