"""User management API tests"""
import pytest
from httpx import AsyncClient
from app.backend.enums.routes import UserRoutes, APIVersionRoutes
from app.backend.utils.pki_generator import PKICertificateGenerator


@pytest.mark.api
@pytest.mark.asyncio
class TestUserEndpoints:
    
    async def test_list_users_success(self, client: AsyncClient, admin_auth_headers):
        """Test listing users with admin permissions"""
        url = f"{UserRoutes.BASE.versioned_path()}/"
        response = await client.get(url, headers=admin_auth_headers)
        
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)

    async def test_list_users_unauthorized(self, client: AsyncClient):
        """Test listing users without authentication"""
        url = f"{UserRoutes.BASE.versioned_path()}/"
        response = await client.get(url)
        
        assert response.status_code == 401

    async def test_list_users_forbidden(self, client: AsyncClient, user_auth_headers):
        """Test listing users with insufficient permissions"""
        url = f"{UserRoutes.BASE.versioned_path()}/"
        response = await client.get(url, headers=user_auth_headers)
        
        assert response.status_code == 403

    async def test_create_user_success(self, client: AsyncClient, admin_auth_headers):
        """Test creating a new user with admin permissions"""
        pki = PKICertificateGenerator.generate_test_certificate("newuser", 4001)
        user_data = {
            "username": "newuser",
            "username_alias": "New User",
            "pki_certificate_cn": pki["cn"],
            "pki_certificate_serial": pki["serial"],
            "pki_certificate_fingerprint": pki["fingerprint"],
            "post_id": None
        }
        
        url = f"{UserRoutes.BASE.versioned_path()}/"
        response = await client.post(url, json=user_data, headers=admin_auth_headers)
        
        assert response.status_code == 200
        data = response.json()
        assert data["username"] == "newuser"
        assert data["username_alias"] == "New User"
        assert data["is_active"] is True

    async def test_create_user_duplicate_username(self, client: AsyncClient, admin_auth_headers, admin_user):
        """Test creating user with duplicate username"""
        pki = PKICertificateGenerator.generate_test_certificate("duplicate", 4002)
        user_data = {
            "username": admin_user.username,  # Duplicate username
            "username_alias": "Duplicate User",
            "pki_certificate_cn": pki["cn"],
            "pki_certificate_serial": pki["serial"],
            "pki_certificate_fingerprint": pki["fingerprint"]
        }
        
        url = f"{UserRoutes.BASE.versioned_path()}/"
        response = await client.post(url, json=user_data, headers=admin_auth_headers)
        
        assert response.status_code == 400
        assert "already exists" in response.json()["detail"]

    async def test_get_user_by_id_success(self, client: AsyncClient, admin_auth_headers, admin_user):
        """Test getting user by ID"""
        url = f"{UserRoutes.USER_BY_ID.versioned_path()}".replace("{user_id}", str(admin_user.id))
        response = await client.get(url, headers=admin_auth_headers)
        
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == admin_user.id
        assert data["username"] == admin_user.username

    async def test_get_user_by_id_not_found(self, client: AsyncClient, admin_auth_headers):
        """Test getting non-existent user"""
        url = f"{UserRoutes.USER_BY_ID.versioned_path()}".replace("{user_id}", "99999")
        response = await client.get(url, headers=admin_auth_headers)
        
        assert response.status_code == 404
        assert "not found" in response.json()["detail"]

    async def test_update_user_success(self, client: AsyncClient, admin_auth_headers, regular_user):
        """Test updating user information"""
        update_data = {
            "username_alias": "Updated User Alias",
            "is_active": True
        }
        
        url = f"{UserRoutes.USER_BY_ID.versioned_path()}".replace("{user_id}", str(regular_user.id))
        response = await client.patch(url, json=update_data, headers=admin_auth_headers)
        
        assert response.status_code == 200
        data = response.json()
        assert data["username_alias"] == "Updated User Alias"
        assert data["is_active"] is True

    async def test_list_users_pagination(self, client: AsyncClient, admin_auth_headers):
        """Test user listing with pagination parameters"""
        url = f"{UserRoutes.BASE.versioned_path()}/"
        response = await client.get(f"{url}?skip=0&limit=10", headers=admin_auth_headers)
        
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) <= 10

    async def test_user_routes_enum_values(self):
        """Test that user route enums return correct paths"""
        assert UserRoutes.BASE.versioned_path() == "/api/v1/users"
        assert UserRoutes.USER_BY_ID.versioned_path() == "/api/v1/users/{user_id}"
        assert UserRoutes.STATISTICS.versioned_path() == "/api/v1/users/statistics"