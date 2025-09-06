"""Authentication API tests"""
import pytest
from httpx import AsyncClient
from app.backend.utils.pki_generator import PKICertificateGenerator
from app.backend.enums.routes import AuthRoutes, APIVersionRoutes


@pytest.mark.auth
@pytest.mark.asyncio
class TestAuthEndpoints:
    
    async def test_login_success(self, client: AsyncClient, test_session):
        """Test successful login with valid PKI credentials"""
        # Create a user specifically for this login test
        from app.backend.utils.pki_generator import PKICertificateGenerator
        from app.backend.services.auth import AuthService
        from app.backend.models.user import User
        import time
        
        unique_serial = int(time.time() * 1000) % (10**8) + 9000
        admin_pki = PKICertificateGenerator.generate_test_certificate(f"login_admin_{unique_serial}", unique_serial)
        
        # Create a user with hashed credentials 
        test_user = User(
            username=f"login_test_user_{unique_serial}",
            username_alias="Login Test User",
            pki_certificate_cn_hash=AuthService.hash_pki_data(admin_pki["cn"]),
            pki_certificate_serial_hash=AuthService.hash_pki_data(admin_pki["serial"]),
            pki_certificate_fingerprint_hash=AuthService.hash_pki_data(admin_pki["fingerprint"]),
            is_active=True
        )
        test_session.add(test_user)
        await test_session.commit()
        
        # Now use the raw PKI data to login (the service will hash it and compare)
        login_data = {
            "certificate_cn": admin_pki["cn"],
            "certificate_serial": admin_pki["serial"], 
            "certificate_fingerprint": admin_pki["fingerprint"]
        }
        
        url = AuthRoutes.LOGIN.versioned_path()
        response = await client.post(url, json=login_data)
        
        assert response.status_code == 200
        data = response.json()
        assert "access_token" in data
        assert "refresh_token" in data
        assert data["token_type"] == "bearer"
        assert data["user_id"] == test_user.id
        assert data["username"] == test_user.username
        assert "expires_in" in data

    async def test_login_invalid_certificate(self, client: AsyncClient):
        """Test login with invalid PKI certificate"""
        login_data = {
            "certificate_cn": "invalid@test.local",
            "certificate_serial": "999999",
            "certificate_fingerprint": "ABCD:EF12:1234:5678"
        }
        
        url = AuthRoutes.LOGIN.versioned_path()
        response = await client.post(url, json=login_data)
        
        assert response.status_code == 401
        assert "Invalid certificate credentials" in response.json()["detail"]

    async def test_login_missing_fields(self, client: AsyncClient):
        """Test login with missing required fields"""
        login_data = {
            "certificate_cn": "test@test.local"
            # Missing certificate_serial and certificate_fingerprint
        }
        
        url = AuthRoutes.LOGIN.versioned_path()
        response = await client.post(url, json=login_data)
        
        assert response.status_code == 422  # Validation error

    async def test_get_current_user_info_success(self, client: AsyncClient, admin_auth_headers):
        """Test getting current user info with valid token"""
        url = AuthRoutes.ME.versioned_path()
        response = await client.get(url, headers=admin_auth_headers)
        
        assert response.status_code == 200
        data = response.json()
        assert "id" in data
        assert "username" in data
        assert "username_alias" in data
        assert "post_id" in data
        assert "is_active" in data

    async def test_get_current_user_info_unauthorized(self, client: AsyncClient):
        """Test getting current user info without token"""
        url = AuthRoutes.ME.versioned_path()
        response = await client.get(url)
        
        assert response.status_code == 401

    async def test_get_current_user_info_invalid_token(self, client: AsyncClient):
        """Test getting current user info with invalid token"""
        headers = {"Authorization": "Bearer invalid_token"}
        url = AuthRoutes.ME.versioned_path()
        response = await client.get(url, headers=headers)
        
        assert response.status_code == 401

    async def test_login_inactive_user(self, client: AsyncClient, test_session):
        """Test login with inactive user"""
        # Create inactive user
        from app.backend.models.user import User
        from app.backend.services.auth import AuthService
        
        pki = PKICertificateGenerator.generate_test_certificate("inactive_user", 3001)
        inactive_user = User(
            username="inactive_user",
            username_alias="Inactive User",
            pki_certificate_cn_hash=AuthService.hash_pki_data(pki["cn"]),
            pki_certificate_serial_hash=AuthService.hash_pki_data(pki["serial"]),
            pki_certificate_fingerprint_hash=AuthService.hash_pki_data(pki["fingerprint"]),
            is_active=False  # Inactive
        )
        test_session.add(inactive_user)
        await test_session.commit()
        
        login_data = {
            "certificate_cn": pki["cn"],
            "certificate_serial": pki["serial"],
            "certificate_fingerprint": pki["fingerprint"]
        }
        
        url = AuthRoutes.LOGIN.versioned_path()
        response = await client.post(url, json=login_data)
        
        assert response.status_code == 401
        assert "Invalid certificate credentials" in response.json()["detail"]
    
    async def test_auth_routes_enum_values(self):
        """Test that enum routes return correct hard-coded API paths"""
        # Verify the actual API paths with hard version
        assert AuthRoutes.LOGIN.versioned_path() == "/api/v1/auth/login"
        assert AuthRoutes.ME.versioned_path() == "/api/v1/auth/me"
        assert AuthRoutes.REFRESH.versioned_path() == "/api/v1/auth/refresh"
        assert AuthRoutes.LOGOUT.versioned_path() == "/api/v1/auth/logout"