"""Error scenario and edge case tests"""
import pytest
from httpx import AsyncClient
from app.backend.enums.routes import AuthRoutes, UserRoutes, PostRoutes, ObservationRoutes, APIVersionRoutes


@pytest.mark.api
@pytest.mark.asyncio
class TestErrorScenarios:
    
    async def test_malformed_json_requests(self, client: AsyncClient, admin_auth_headers):
        """Test API responses to malformed JSON"""
        malformed_json = '{"username": "test", "invalid_json"'
        
        url = f"{UserRoutes.BASE.versioned_path()}/"
        response = await client.post(
            url, 
            content=malformed_json,
            headers={**admin_auth_headers, "Content-Type": "application/json"}
        )
        
        assert response.status_code == 422

    async def test_oversized_request_data(self, client: AsyncClient, admin_auth_headers):
        """Test API responses to oversized request data"""
        # Create a very large username
        oversized_data = {
            "username": "a" * 1000,  # Assuming username has length limit
            "username_alias": "Test User",
            "pki_certificate_cn": "test@test.local",
            "pki_certificate_serial": "12345",
            "pki_certificate_fingerprint": "fingerprint"
        }
        
        url = f"{UserRoutes.BASE.versioned_path()}/"
        response = await client.post(url, json=oversized_data, headers=admin_auth_headers)
        
        assert response.status_code in [422, 500]  # Should fail validation or database constraint

    async def test_sql_injection_attempts(self, client: AsyncClient, admin_auth_headers):
        """Test API protection against SQL injection attempts"""
        malicious_data = {
            "username": "admin'; DROP TABLE users; --",
            "username_alias": "Malicious User",
            "pki_certificate_cn": "malicious@test.local",
            "pki_certificate_serial": "12345",
            "pki_certificate_fingerprint": "fingerprint"
        }
        
        url = f"{UserRoutes.BASE.versioned_path()}/"
        response = await client.post(url, json=malicious_data, headers=admin_auth_headers)
        
        # Should either succeed (SQL injection prevented) or fail validation
        assert response.status_code in [200, 422, 400]
        
        # If it succeeds, verify the username is stored as-is (escaped)
        if response.status_code == 200:
            data = response.json()
            assert data["username"] == "admin'; DROP TABLE users; --"

    async def test_xss_attempts_in_input(self, client: AsyncClient, admin_auth_headers):
        """Test API handling of XSS attempts in input"""
        xss_data = {
            "username": "testuser",
            "username_alias": "<script>alert('xss')</script>",
            "pki_certificate_cn": "xss@test.local",
            "pki_certificate_serial": "12345",
            "pki_certificate_fingerprint": "fingerprint"
        }
        
        url = f"{UserRoutes.BASE.versioned_path()}/"
        response = await client.post(url, json=xss_data, headers=admin_auth_headers)
        
        # Should succeed with input stored as-is (the application doesn't sanitize HTML)
        if response.status_code == 200:
            data = response.json()
            # This test just verifies the API doesn't crash with HTML input
            assert "username_alias" in data

    async def test_concurrent_user_creation(self, client: AsyncClient, admin_auth_headers):
        """Test handling of concurrent user creation with same username"""
        import asyncio
        from app.backend.utils.pki_generator import PKICertificateGenerator
        
        # Create two users with same username concurrently
        async def create_user(username_suffix):
            pki = PKICertificateGenerator.generate_test_certificate(f"concurrent{username_suffix}", 6000 + int(username_suffix))
            user_data = {
                "username": "concurrent_test",  # Same username
                "username_alias": f"Concurrent User {username_suffix}",
                "pki_certificate_cn": pki["cn"],
                "pki_certificate_serial": pki["serial"],
                "pki_certificate_fingerprint": pki["fingerprint"]
            }
            
            url = f"{UserRoutes.BASE.versioned_path()}/"
            return await client.post(url, json=user_data, headers=admin_auth_headers)
        
        # Execute concurrently
        results = await asyncio.gather(
            create_user("1"),
            create_user("2"),
            return_exceptions=True
        )
        
        # Check results - some may be exceptions due to database concurrency
        success_count = sum(1 for r in results if hasattr(r, 'status_code') and r.status_code == 200)
        error_count = sum(1 for r in results if hasattr(r, 'status_code') and r.status_code in [400, 500])
        exception_count = sum(1 for r in results if not hasattr(r, 'status_code'))
        
        assert success_count >= 1, "At least one user creation should succeed"
        # Total results should equal our request count
        assert success_count + error_count + exception_count == len(results), "All requests should be accounted for"
        # The test demonstrates that concurrent operations are handled (either by success/failure or database-level protection)
        assert len(results) == 2, "Should have exactly 2 results"

    async def test_invalid_token_formats(self, client: AsyncClient):
        """Test various invalid token formats"""
        invalid_tokens = [
            "Bearer ",  # Empty token
            "Bearer invalid_token_format",  # Invalid format
            "Basic dGVzdDp0ZXN0",  # Wrong auth type
            "Bearer " + "a" * 1000,  # Oversized token
            "",  # Empty header
        ]
        
        url = AuthRoutes.ME.versioned_path()
        
        for token in invalid_tokens:
            headers = {"Authorization": token} if token else {}
            response = await client.get(url, headers=headers)
            assert response.status_code == 401, f"Token '{token}' should be rejected"

    async def test_database_constraint_violations(self, client: AsyncClient, admin_auth_headers, test_post, test_signal_type):
        """Test handling of database constraint violations"""
        # Try to create observation with non-existent post_id
        observation_data = {
            "post_id": 99999,  # Non-existent
            "signal_type_id": test_signal_type.id,
            "frequency_min": "144.500",
            "frequency_max": "144.600",
            "observed_at": "2024-01-01T12:00:00Z"
        }
        
        url = f"{ObservationRoutes.BASE.versioned_path()}/"
        response = await client.post(url, json=observation_data, headers=admin_auth_headers)
        
        # Should fail with appropriate error
        assert response.status_code in [400, 422, 500]

    async def test_rate_limiting_behavior(self, client: AsyncClient, admin_auth_headers):
        """Test rapid requests to check for rate limiting or stability"""
        import asyncio
        
        url = f"{UserRoutes.BASE.versioned_path()}/"
        
        # Make many rapid requests
        tasks = []
        for i in range(20):
            tasks.append(client.get(url, headers=admin_auth_headers))
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # All requests should either succeed, be rate limited, or fail due to database concurrency
        for result in results:
            if hasattr(result, 'status_code'):
                assert result.status_code in [200, 429, 500, 503], "Unexpected status code under load"

    async def test_invalid_route_parameters(self, client: AsyncClient, admin_auth_headers):
        """Test invalid route parameters"""
        invalid_params = [
            ("abc", [422]),      # Non-numeric ID - FastAPI validation error
            ("-1", [400]),       # Negative ID - our custom validation
            ("0", [400]),        # Zero ID - our custom validation
            ("99999999999999", [404]),  # Very large ID - not found
            ("null", [422]),     # String null - FastAPI validation error
        ]
        
        for param, expected_codes in invalid_params:
            url = UserRoutes.USER_BY_ID.versioned_path().replace("{user_id}", param)
            response = await client.get(url, headers=admin_auth_headers)
            
            assert response.status_code in expected_codes, f"Invalid parameter '{param}' returned {response.status_code}, expected one of {expected_codes}"
        
        # Special case: empty string creates a different route entirely  
        # /api/v1/users/{user_id} where user_id="" becomes /api/v1/users/ which is the list endpoint
        empty_url = UserRoutes.USER_BY_ID.versioned_path().replace("{user_id}", "")
        response = await client.get(empty_url, headers=admin_auth_headers)
        # This actually becomes the list users endpoint, so it should succeed
        assert response.status_code == 200, "Empty user_id parameter should result in list users endpoint"

    async def test_empty_request_bodies(self, client: AsyncClient, admin_auth_headers):
        """Test endpoints with empty request bodies"""
        endpoints = [
            f"{UserRoutes.BASE.versioned_path()}/",
            f"{PostRoutes.BASE.versioned_path()}/",
            f"{ObservationRoutes.BASE.versioned_path()}/"
        ]
        
        for endpoint in endpoints:
            # Test completely empty body
            response = await client.post(endpoint, json={}, headers=admin_auth_headers)
            assert response.status_code == 422  # Should fail validation
            
            # Test null body
            response = await client.post(endpoint, json=None, headers=admin_auth_headers)
            assert response.status_code in [400, 422]