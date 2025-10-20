"""Health check API tests"""
import pytest
from httpx import AsyncClient
from app.backend.enums.routes import SystemRoutes, APIVersionRoutes


@pytest.mark.api
@pytest.mark.asyncio
class TestHealthEndpoints:
    
    async def test_health_check_success(self, client: AsyncClient):
        """Test health check endpoint returns 200"""
        url = SystemRoutes.HEALTH.path()  # Health doesn't use versioned API
        response = await client.get(url)
        
        assert response.status_code == 200
        data = response.json()
        assert "status" in data
        assert "timestamp" in data
        assert "version" in data
        assert data["status"] in ["healthy", "degraded", "unhealthy"]

    async def test_health_check_includes_database_status(self, client: AsyncClient):
        """Test health check includes database connectivity status"""
        url = SystemRoutes.HEALTH.path()
        response = await client.get(url)
        
        assert response.status_code == 200
        data = response.json()
        assert "database" in data
        assert "status" in data["database"]

    async def test_health_check_response_format(self, client: AsyncClient):
        """Test health check response has correct format"""
        url = SystemRoutes.HEALTH.path()
        response = await client.get(url)
        
        assert response.status_code == 200
        data = response.json()
        
        # Check required fields
        required_fields = ["status", "timestamp", "version", "database"]
        for field in required_fields:
            assert field in data, f"Missing required field: {field}"
        
        # Check database sub-fields
        assert "status" in data["database"]
        assert "response_time" in data["database"]

    async def test_system_routes_enum_values(self):
        """Test that system route enums return correct paths"""
        assert SystemRoutes.HEALTH.path() == "/health"
        assert SystemRoutes.ROOT.path() == "/"
        assert SystemRoutes.OPENAPI_JSON.path() == "/openapi.json"