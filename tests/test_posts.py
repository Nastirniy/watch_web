"""Post management API tests"""
import pytest
from httpx import AsyncClient
from app.backend.enums.routes import PostRoutes, APIVersionRoutes


@pytest.mark.api
@pytest.mark.asyncio
class TestPostEndpoints:
    
    async def test_list_posts_success(self, client: AsyncClient, admin_auth_headers):
        """Test listing posts with admin permissions"""
        url = f"{PostRoutes.BASE.versioned_path()}/"
        response = await client.get(url, headers=admin_auth_headers)
        
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)

    async def test_list_posts_unauthorized(self, client: AsyncClient):
        """Test listing posts without authentication"""
        url = f"{PostRoutes.BASE.versioned_path()}/"
        response = await client.get(url)
        
        assert response.status_code == 401

    async def test_create_post_success(self, client: AsyncClient, admin_auth_headers):
        """Test creating a new post"""
        import time
        unique_code = f"TESTPOST{int(time.time() * 1000) % (10**6)}"
        post_data = {
            "name": "Test Monitoring Post",
            "code": unique_code,
            "description": "Test post for monitoring signals",
            "region": "Test Region"
        }
        
        url = f"{PostRoutes.BASE.versioned_path()}/"
        response = await client.post(url, json=post_data, headers=admin_auth_headers)
        
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Test Monitoring Post"
        assert data["code"] == unique_code
        assert data["region"] == "Test Region"
        assert data["is_active"] is True

    async def test_create_post_duplicate_code(self, client: AsyncClient, admin_auth_headers, test_post):
        """Test creating post with duplicate code"""
        post_data = {
            "name": "Another Test Post",
            "code": test_post.code,  # Duplicate code
            "description": "Another test post",
            "region": "Another Region"
        }
        
        url = f"{PostRoutes.BASE.versioned_path()}/"
        response = await client.post(url, json=post_data, headers=admin_auth_headers)
        
        assert response.status_code == 400
        assert "already exists" in response.json()["detail"]

    async def test_get_post_by_id_success(self, client: AsyncClient, admin_auth_headers, test_post):
        """Test getting post by ID"""
        url = f"{PostRoutes.POST_BY_ID.versioned_path()}".replace("{post_id}", str(test_post.id))
        response = await client.get(url, headers=admin_auth_headers)
        
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == test_post.id
        assert data["name"] == test_post.name
        assert data["code"] == test_post.code

    async def test_get_post_by_id_not_found(self, client: AsyncClient, admin_auth_headers):
        """Test getting non-existent post"""
        url = f"{PostRoutes.POST_BY_ID.versioned_path()}".replace("{post_id}", "99999")
        response = await client.get(url, headers=admin_auth_headers)
        
        assert response.status_code == 404
        assert "not found" in response.json()["detail"]

    async def test_list_posts_with_filters(self, client: AsyncClient, admin_auth_headers):
        """Test listing posts with filter parameters"""
        url = f"{PostRoutes.BASE.versioned_path()}/"
        response = await client.get(f"{url}?is_active=true&skip=0&limit=10", headers=admin_auth_headers)
        
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)

    async def test_create_post_missing_required_fields(self, client: AsyncClient, admin_auth_headers):
        """Test creating post with missing required fields"""
        post_data = {
            "name": "Incomplete Post"
            # Missing required 'code' field
        }
        
        url = f"{PostRoutes.BASE.versioned_path()}/"
        response = await client.post(url, json=post_data, headers=admin_auth_headers)
        
        assert response.status_code == 422  # Validation error

    async def test_create_post_forbidden(self, client: AsyncClient, user_auth_headers):
        """Test creating post with insufficient permissions"""
        import time
        unique_code = f"FORBIDDEN{int(time.time() * 1000) % (10**6)}"
        post_data = {
            "name": "Forbidden Post",
            "code": unique_code,
            "description": "Should not be created",
            "region": "Test Region"
        }
        
        url = f"{PostRoutes.BASE.versioned_path()}/"
        response = await client.post(url, json=post_data, headers=user_auth_headers)
        
        assert response.status_code == 403

    async def test_post_routes_enum_values(self):
        """Test that post route enums return correct paths"""
        assert f"{PostRoutes.BASE.versioned_path()}/" == "/api/v1/posts/"
        assert f"{PostRoutes.POST_BY_ID.versioned_path()}" == "/api/v1/posts/{post_id}"
        assert f"{PostRoutes.ACTIVE.versioned_path()}" == "/api/v1/posts/active"
        assert f"{PostRoutes.STATISTICS.versioned_path()}" == "/api/v1/posts/statistics"