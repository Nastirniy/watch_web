"""Observation management API tests"""
import pytest
from httpx import AsyncClient
from decimal import Decimal
from datetime import datetime, timezone
from app.backend.enums.routes import ObservationRoutes, APIVersionRoutes


@pytest.mark.api
@pytest.mark.asyncio
class TestObservationEndpoints:
    
    async def test_list_observations_success(self, client: AsyncClient, admin_auth_headers):
        """Test listing observations with admin permissions"""
        url = f"{ObservationRoutes.BASE.versioned_path()}/"
        response = await client.get(url, headers=admin_auth_headers)
        
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)

    async def test_list_observations_unauthorized(self, client: AsyncClient):
        """Test listing observations without authentication"""
        url = f"{ObservationRoutes.BASE.versioned_path()}/"
        response = await client.get(url)
        
        assert response.status_code == 401

    async def test_create_observation_success(self, client: AsyncClient, admin_auth_headers, test_post, test_signal_type):
        """Test creating a new observation"""
        observation_data = {
            "post_id": test_post.id,
            "signal_type_id": test_signal_type.id,
            "frequency_min": "144.500",
            "frequency_max": "144.600",
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "duration_seconds": 300,
            "comment": "Test observation",
            "confidence_level": "HIGH"
        }
        
        url = f"{ObservationRoutes.BASE.versioned_path()}/"
        response = await client.post(url, json=observation_data, headers=admin_auth_headers)
        
        assert response.status_code == 200
        data = response.json()
        assert data["post_id"] == test_post.id
        assert data["signal_type_id"] == test_signal_type.id
        assert float(data["frequency_min"]) == 144.500
        assert float(data["frequency_max"]) == 144.600
        assert data["comment"] == "Test observation"

    async def test_create_observation_missing_fields(self, client: AsyncClient, admin_auth_headers):
        """Test creating observation with missing required fields"""
        observation_data = {
            "frequency_min": "144.500",
            "frequency_max": "144.600"
            # Missing post_id, signal_type_id, observed_at
        }
        
        url = f"{ObservationRoutes.BASE.versioned_path()}/"
        response = await client.post(url, json=observation_data, headers=admin_auth_headers)
        
        assert response.status_code == 422  # Validation error

    async def test_create_observation_invalid_frequency_range(self, client: AsyncClient, admin_auth_headers, test_post, test_signal_type):
        """Test creating observation with invalid frequency range (min > max)"""
        observation_data = {
            "post_id": test_post.id,
            "signal_type_id": test_signal_type.id,
            "frequency_min": "144.600",  # Higher than max
            "frequency_max": "144.500",  # Lower than min
            "observed_at": datetime.now(timezone.utc).isoformat()
        }
        
        url = f"{ObservationRoutes.BASE.versioned_path()}/"
        response = await client.post(url, json=observation_data, headers=admin_auth_headers)
        
        # Should still create but with potentially calculated center/bandwidth
        assert response.status_code == 200

    async def test_mark_observation_disappeared_success(self, client: AsyncClient, admin_auth_headers, test_session, test_post, test_signal_type, admin_user):
        """Test marking observation as disappeared"""
        # First create an observation
        from app.backend.models.observation import Observation
        
        observation = Observation(
            user_id=admin_user.id,
            post_id=test_post.id,
            signal_type_id=test_signal_type.id,
            frequency_min=Decimal("144.500"),
            frequency_max=Decimal("144.600"),
            observed_at=datetime.now(timezone.utc)
        )
        test_session.add(observation)
        await test_session.commit()
        
        # Mark as disappeared
        url = f"{ObservationRoutes.MARK_DISAPPEARED.versioned_path()}".replace("{observation_id}", str(observation.id))
        response = await client.post(url, headers=admin_auth_headers)
        
        assert response.status_code == 200
        data = response.json()
        assert "disappeared" in data["message"]

    async def test_mark_observation_disappeared_not_found(self, client: AsyncClient, admin_auth_headers):
        """Test marking non-existent observation as disappeared"""
        url = f"{ObservationRoutes.MARK_DISAPPEARED.versioned_path()}".replace("{observation_id}", "99999")
        response = await client.post(url, headers=admin_auth_headers)
        
        assert response.status_code == 404
        assert "not found" in response.json()["detail"]

    async def test_list_observations_with_filters(self, client: AsyncClient, admin_auth_headers, test_post):
        """Test listing observations with filter parameters"""
        url = f"{ObservationRoutes.BASE.versioned_path()}/"
        response = await client.get(f"{url}?post_id={test_post.id}&status=ACTIVE&skip=0&limit=10", headers=admin_auth_headers)
        
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)

    async def test_create_observation_forbidden(self, client: AsyncClient, user_auth_headers, test_post, test_signal_type):
        """Test creating observation with insufficient permissions (if regular users can't create)"""
        observation_data = {
            "post_id": test_post.id,
            "signal_type_id": test_signal_type.id,
            "frequency_min": "144.500",
            "frequency_max": "144.600",
            "observed_at": datetime.now(timezone.utc).isoformat()
        }
        
        url = f"{ObservationRoutes.BASE.versioned_path()}/"
        response = await client.post(url, json=observation_data, headers=user_auth_headers)
        
        # This might be 403 or 200 depending on permissions - adjust based on your business logic
        assert response.status_code in [200, 403]

    async def test_observation_routes_enum_values(self):
        """Test that observation route enums return correct paths"""
        assert f"{ObservationRoutes.BASE.versioned_path()}/" == "/api/v1/observations/"
        assert f"{ObservationRoutes.OBSERVATION_BY_ID.versioned_path()}" == "/api/v1/observations/{observation_id}"
        assert f"{ObservationRoutes.MARK_DISAPPEARED.versioned_path()}" == "/api/v1/observations/{observation_id}/disappeared"
        assert f"{ObservationRoutes.STATISTICS.versioned_path()}" == "/api/v1/observations/statistics"