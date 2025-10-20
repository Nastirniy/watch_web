"""Integration tests for complete workflows"""
import pytest
from httpx import AsyncClient
from datetime import datetime, timezone
from app.backend.enums.routes import AuthRoutes, UserRoutes, PostRoutes, ObservationRoutes, APIVersionRoutes
from app.backend.utils.pki_generator import PKICertificateGenerator


@pytest.mark.integration
@pytest.mark.asyncio
class TestCompleteWorkflows:
    
    async def test_full_user_lifecycle(self, client: AsyncClient, admin_auth_headers):
        """Test complete user lifecycle: create, login, use API, update, deactivate"""
        
        # Step 1: Create a new user
        pki = PKICertificateGenerator.generate_test_certificate("lifecycle_user", 5001)
        user_data = {
            "username": "lifecycle_user",
            "username_alias": "Lifecycle Test User",
            "pki_certificate_cn": pki["cn"],
            "pki_certificate_serial": pki["serial"],
            "pki_certificate_fingerprint": pki["fingerprint"]
        }
        
        create_url = f"{UserRoutes.BASE.versioned_path()}/"
        response = await client.post(create_url, json=user_data, headers=admin_auth_headers)
        assert response.status_code == 200
        user = response.json()
        user_id = user["id"]
        
        # Step 2: Login as the new user
        login_data = {
            "certificate_cn": pki["cn"],
            "certificate_serial": pki["serial"],
            "certificate_fingerprint": pki["fingerprint"]
        }
        
        login_url = AuthRoutes.LOGIN.versioned_path()
        response = await client.post(login_url, json=login_data)
        assert response.status_code == 200
        login_response = response.json()
        user_token = login_response["access_token"]
        user_headers = {"Authorization": f"Bearer {user_token}"}
        
        # Step 3: Use API as the new user (get own info)
        me_url = AuthRoutes.ME.versioned_path()
        response = await client.get(me_url, headers=user_headers)
        assert response.status_code == 200
        me_data = response.json()
        assert me_data["username"] == "lifecycle_user"
        
        # Step 4: Update user (as admin)
        update_data = {"username_alias": "Updated Lifecycle User"}
        update_url = UserRoutes.USER_BY_ID.versioned_path().replace("{user_id}", str(user_id))
        response = await client.patch(update_url, json=update_data, headers=admin_auth_headers)
        assert response.status_code == 200
        updated_user = response.json()
        assert updated_user["username_alias"] == "Updated Lifecycle User"
        
        # Step 5: Deactivate user (as admin)
        deactivate_data = {"is_active": False}
        response = await client.patch(update_url, json=deactivate_data, headers=admin_auth_headers)
        assert response.status_code == 200
        
        # Step 6: Verify deactivated user cannot login
        response = await client.post(login_url, json=login_data)
        assert response.status_code == 401

    async def test_observation_workflow(self, client: AsyncClient, admin_auth_headers, test_post, test_signal_type):
        """Test complete observation workflow: create, list, mark disappeared"""
        
        # Step 1: Create observation
        observation_data = {
            "post_id": test_post.id,
            "signal_type_id": test_signal_type.id,
            "frequency_min": "145.000",
            "frequency_max": "145.100",
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "comment": "Integration test observation"
        }
        
        create_url = f"{ObservationRoutes.BASE.versioned_path()}/"
        response = await client.post(create_url, json=observation_data, headers=admin_auth_headers)
        assert response.status_code == 200
        observation = response.json()
        observation_id = observation["id"]
        
        # Step 2: List observations and verify it's there
        list_url = f"{ObservationRoutes.BASE.versioned_path()}/"
        response = await client.get(list_url, headers=admin_auth_headers)
        assert response.status_code == 200
        observations = response.json()
        assert len(observations) >= 1
        
        # Find our observation
        our_observation = next((obs for obs in observations if obs["id"] == observation_id), None)
        assert our_observation is not None
        assert our_observation["comment"] == "Integration test observation"
        
        # Step 3: Mark observation as disappeared
        disappeared_url = ObservationRoutes.MARK_DISAPPEARED.versioned_path().replace("{observation_id}", str(observation_id))
        response = await client.post(disappeared_url, headers=admin_auth_headers)
        assert response.status_code == 200

    async def test_post_and_observation_relationship(self, client: AsyncClient, admin_auth_headers, test_signal_type):
        """Test creating post and associated observations"""
        
        # Step 1: Create a new post
        import time
        unique_code = f"INTTEST{int(time.time() * 1000) % (10**6)}"
        post_data = {
            "name": "Integration Test Post",
            "code": unique_code,
            "description": "Post for integration testing",
            "region": "Test Integration Region"
        }
        
        create_post_url = f"{PostRoutes.BASE.versioned_path()}/"
        response = await client.post(create_post_url, json=post_data, headers=admin_auth_headers)
        assert response.status_code == 200
        post = response.json()
        post_id = post["id"]
        
        # Step 2: Create multiple observations for this post
        for i in range(3):
            observation_data = {
                "post_id": post_id,
                "signal_type_id": test_signal_type.id,
                "frequency_min": f"14{4 + i}.000",
                "frequency_max": f"14{4 + i}.100",
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "comment": f"Integration test observation {i + 1}"
            }
            
            create_obs_url = f"{ObservationRoutes.BASE.versioned_path()}/"
            response = await client.post(create_obs_url, json=observation_data, headers=admin_auth_headers)
            assert response.status_code == 200
        
        # Step 3: List observations for this specific post
        list_url = f"{ObservationRoutes.BASE.versioned_path()}/"
        response = await client.get(f"{list_url}?post_id={post_id}", headers=admin_auth_headers)
        assert response.status_code == 200
        post_observations = response.json()
        
        # Should have at least 3 observations for this post
        assert len(post_observations) >= 3
        
        # All observations should belong to our post
        for obs in post_observations:
            assert obs["post_id"] == post_id

    async def test_permission_cascading(self, client: AsyncClient, admin_auth_headers, user_auth_headers):
        """Test that permissions work correctly across different endpoints"""
        
        # Admin should be able to access all endpoints
        endpoints_to_test = [
            f"{UserRoutes.BASE.versioned_path()}/",
            f"{PostRoutes.BASE.versioned_path()}/",
            f"{ObservationRoutes.BASE.versioned_path()}/"
        ]
        
        for endpoint in endpoints_to_test:
            response = await client.get(endpoint, headers=admin_auth_headers)
            assert response.status_code == 200, f"Admin should access {endpoint}"
        
        # Regular user should be denied access to user management
        response = await client.get(f"{UserRoutes.BASE.versioned_path()}/", headers=user_auth_headers)
        assert response.status_code == 403, "Regular user should not access user management"
        
        # But regular user should access their own info
        response = await client.get(AuthRoutes.ME.versioned_path(), headers=user_auth_headers)
        assert response.status_code == 200, "Regular user should access own info"