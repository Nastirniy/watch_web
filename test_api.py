"""
Frontend Application Simulator - Real User Workflow Testing
This simulates how a real frontend application would interact with the API
"""

import asyncio
import json
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
import httpx
from app.backend.enums.routes import (
    AuthRoutes, UserRoutes, ObservationRoutes, 
    PostRoutes, SystemRoutes, APIVersionRoutes
)

# App configuration (like frontend config)
class AppConfig:
    BASE_URL = "http://localhost:60800"
    API_VERSION = APIVersionRoutes.V1.value
    TIMEOUT = 30.0
    DEFAULT_PAGE_SIZE = 20

class FrontendApp:
    """Simulates a real frontend application"""
    
    def __init__(self):
        self.config = AppConfig()
        self.user_session = {
            'access_token': None,
            'refresh_token': None,
            'user_id': None,
            'username': None,
            'is_authenticated': False
        }
        self.app_state = {
            'current_page': 'login',
            'users': [],
            'posts': [],
            'observations': [],
            'selected_post': None,
            'notifications': []
        }
    
    def log(self, level: str, message: str):
        """Application logging (like frontend console.log)"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        print(f"[{timestamp}] {level.upper()}: {message}")
    
    def show_notification(self, message: str, type: str = "info"):
        """Show user notification (like frontend toast/alert)"""
        notification = {
            'message': message,
            'type': type,
            'timestamp': datetime.now().isoformat()
        }
        self.app_state['notifications'].append(notification)
        icon = "[SUCCESS]" if type == "success" else "[ERROR]" if type == "error" else "[INFO]"
        print(f"\n{icon} {message}")
    
    async def api_request(self, method: str, endpoint: str, data: dict = None, params: dict = None) -> tuple[bool, dict]:
        """Make API request with proper error handling (like frontend axios/fetch)"""
        url = f"{self.config.BASE_URL}{endpoint}"
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        
        # Add auth token if user is logged in
        if self.user_session['is_authenticated'] and self.user_session['access_token']:
            headers["Authorization"] = f"Bearer {self.user_session['access_token']}"
        
        try:
            async with httpx.AsyncClient(timeout=self.config.TIMEOUT) as client:
                response = await client.request(
                    method=method,
                    url=url,
                    headers=headers,
                    json=data if data else None,
                    params=params if params else None
                )
                
                if response.status_code == 200 or response.status_code == 201:
                    return True, response.json()
                elif response.status_code == 401:
                    # Token expired - try to refresh
                    if await self.refresh_token():
                        # Retry with new token
                        headers["Authorization"] = f"Bearer {self.user_session['access_token']}"
                        response = await client.request(method, url, headers=headers, json=data, params=params)
                        if response.status_code == 200:
                            return True, response.json()
                    
                    # Refresh failed, logout user
                    await self.logout()
                    return False, {"error": "Session expired, please login again"}
                else:
                    try:
                        error_data = response.json()
                    except:
                        error_data = {"error": response.text or f"HTTP {response.status_code}"}
                    
                    return False, error_data
                    
        except httpx.TimeoutException:
            self.log("error", "Request timeout")
            return False, {"error": "Request timeout - please check your connection"}
        except Exception as e:
            self.log("error", f"Network error: {e}")
            return False, {"error": f"Network error: {str(e)}"}
    
    async def login(self, certificate_cn: str, certificate_serial: str, certificate_fingerprint: str) -> bool:
        """User login flow (like frontend login form)"""
        self.log("info", f"Attempting login for user: {certificate_cn}")
        self.app_state['current_page'] = 'login'
        
        # Validate input (frontend validation)
        if not all([certificate_cn, certificate_serial, certificate_fingerprint]):
            self.show_notification("Please fill in all certificate fields", "error")
            return False
        
        # Make login request
        endpoint = f"{self.config.API_VERSION}{AuthRoutes.BASE.value}{AuthRoutes.LOGIN.value}"
        login_data = {
            "certificate_cn": certificate_cn,
            "certificate_serial": certificate_serial,
            "certificate_fingerprint": certificate_fingerprint
        }
        
        success, response = await self.api_request("POST", endpoint, login_data)
        
        if success:
            # Store session data (like frontend localStorage/sessionStorage)
            self.user_session.update({
                'access_token': response['access_token'],
                'refresh_token': response['refresh_token'],
                'user_id': response['user_id'],
                'username': response['username'],
                'is_authenticated': True
            })
            
            self.show_notification(f"Welcome back, {response['username']}!", "success")
            self.app_state['current_page'] = 'dashboard'
            await self.load_dashboard_data()
            return True
        else:
            error_msg = response.get('error', response.get('detail', 'Login failed'))
            self.show_notification(f"Login failed: {error_msg}", "error")
            return False
    
    async def refresh_token(self) -> bool:
        """Refresh access token (automatic background process)"""
        if not self.user_session['refresh_token']:
            return False
        
        endpoint = f"{self.config.API_VERSION}{AuthRoutes.BASE.value}{AuthRoutes.REFRESH.value}"
        refresh_data = {"refresh_token": self.user_session['refresh_token']}
        
        success, response = await self.api_request("POST", endpoint, refresh_data)
        
        if success:
            self.user_session.update({
                'access_token': response['access_token'],
                'refresh_token': response['refresh_token']
            })
            self.log("info", "Token refreshed successfully")
            return True
        else:
            self.log("error", "Token refresh failed")
            return False
    
    async def logout(self):
        """User logout (like frontend logout button)"""
        if self.user_session['is_authenticated']:
            endpoint = f"{self.config.API_VERSION}{AuthRoutes.BASE.value}{AuthRoutes.LOGOUT.value}"
            await self.api_request("POST", endpoint, {})
        
        # Clear session data
        self.user_session = {
            'access_token': None,
            'refresh_token': None,
            'user_id': None,
            'username': None,
            'is_authenticated': False
        }
        self.app_state = {
            'current_page': 'login',
            'users': [],
            'posts': [],
            'observations': [],
            'selected_post': None,
            'notifications': []
        }
        
        self.show_notification("You have been logged out", "info")
    
    async def load_dashboard_data(self):
        """Load initial dashboard data (like frontend app initialization)"""
        self.log("info", "Loading dashboard data...")
        
        # Load user info
        await self.load_user_profile()
        
        # Load posts for the dashboard
        await self.load_posts_list()
        
        # Load recent observations
        await self.load_observations_list(limit=5)
        
        self.show_notification("Dashboard loaded successfully", "success")
    
    async def load_user_profile(self):
        """Get current user profile (like profile page)"""
        endpoint = f"{self.config.API_VERSION}{AuthRoutes.BASE.value}{AuthRoutes.ME.value}"
        success, response = await self.api_request("GET", endpoint)
        
        if success:
            self.app_state['user_profile'] = response
            self.log("info", f"User profile loaded: {response.get('username')}")
        else:
            self.show_notification("Failed to load user profile", "error")
    
    async def load_posts_list(self, page: int = 1):
        """Load posts list (like posts management page)"""
        self.log("info", f"Loading posts (page {page})...")
        
        endpoint = f"{self.config.API_VERSION}{PostRoutes.BASE.value}/"
        params = {
            'skip': (page - 1) * self.config.DEFAULT_PAGE_SIZE,
            'limit': self.config.DEFAULT_PAGE_SIZE,
            'is_active': True
        }
        
        success, response = await self.api_request("GET", endpoint, params=params)
        
        if success:
            if page == 1:
                self.app_state['posts'] = response
            else:
                self.app_state['posts'].extend(response)
            
            self.log("info", f"Loaded {len(response)} posts")
            return response
        else:
            error_msg = response.get('error', response.get('detail', 'Failed to load posts'))
            self.show_notification(f"Error loading posts: {error_msg}", "error")
            return []
    
    async def create_new_post(self, name: str, code: str, description: str = "", region: str = ""):
        """Create new post (like add post form)"""
        self.log("info", f"Creating new post: {name}")
        
        # Frontend validation
        if not name or not code:
            self.show_notification("Post name and code are required", "error")
            return False
        
        endpoint = f"{self.config.API_VERSION}{PostRoutes.BASE.value}/"
        post_data = {
            "name": name,
            "code": code,
            "description": description,
            "region": region
        }
        
        success, response = await self.api_request("POST", endpoint, post_data)
        
        if success:
            self.app_state['posts'].insert(0, response)  # Add to beginning of list
            self.show_notification(f"Post '{name}' created successfully", "success")
            return True
        else:
            error_msg = response.get('error', response.get('detail', 'Failed to create post'))
            self.show_notification(f"Error creating post: {error_msg}", "error")
            return False
    
    async def load_observations_list(self, post_id: int = None, limit: int = 20):
        """Load observations (like observations page)"""
        self.log("info", f"Loading observations for post {post_id if post_id else 'all'}")
        
        endpoint = f"{self.config.API_VERSION}{ObservationRoutes.BASE.value}/"
        params = {'limit': limit}
        
        if post_id:
            params['post_id'] = post_id
        
        success, response = await self.api_request("GET", endpoint, params=params)
        
        if success:
            self.app_state['observations'] = response
            self.log("info", f"Loaded {len(response)} observations")
            return response
        else:
            error_msg = response.get('error', response.get('detail', 'Failed to load observations'))
            self.show_notification(f"Error loading observations: {error_msg}", "error")
            return []
    
    async def create_observation(self, post_id: int, frequency_min: float, frequency_max: float, 
                                signal_type_id: int = 1, comment: str = ""):
        """Create new observation (like add observation form)"""
        self.log("info", f"Creating observation for post {post_id}")
        
        # Frontend validation
        if not post_id or frequency_min >= frequency_max:
            self.show_notification("Invalid observation data", "error")
            return False
        
        endpoint = f"{self.config.API_VERSION}{ObservationRoutes.BASE.value}/"
        obs_data = {
            "post_id": post_id,
            "signal_type_id": signal_type_id,  # Now we have valid signal types (1-12)
            "frequency_min": str(frequency_min),
            "frequency_max": str(frequency_max),
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "duration_seconds": 300,
            "signal_strength": str(-75.5),
            "modulation_type_id": 1,  # FM modulation type
            "comment": comment,
            "confidence_level": "medium"
        }
        
        success, response = await self.api_request("POST", endpoint, obs_data)
        
        if success:
            self.app_state['observations'].insert(0, response)
            self.show_notification("Observation recorded successfully", "success")
            return True
        else:
            error_msg = response.get('error', response.get('detail', 'Failed to create observation'))
            self.show_notification(f"Error creating observation: {error_msg}", "error")
            return False
    
    async def load_users_list(self, page: int = 1):
        """Load users list (like user management page)"""
        self.log("info", f"Loading users (page {page})...")
        
        endpoint = f"{self.config.API_VERSION}{UserRoutes.BASE.value}/"
        params = {
            'page': page,
            'size': self.config.DEFAULT_PAGE_SIZE
        }
        
        success, response = await self.api_request("GET", endpoint, params=params)
        
        if success:
            self.app_state['users'] = response
            self.log("info", f"Loaded {len(response)} users")
            return response
        else:
            error_msg = response.get('error', response.get('detail', 'Failed to load users'))
            self.show_notification(f"Error loading users: {error_msg}", "error")
            return []
    
    async def check_system_health(self):
        """Check system health (like admin dashboard)"""
        endpoint = SystemRoutes.HEALTH.value
        success, response = await self.api_request("GET", endpoint)
        
        if success:
            status = response.get('status', 'unknown')
            db_status = response.get('database', {}).get('status', 'unknown')
            
            if status == 'healthy' and db_status == 'healthy':
                self.show_notification("System is running normally", "success")
            else:
                self.show_notification(f"System status: {status}, DB: {db_status}", "error")
            
            return response
        else:
            self.show_notification("Unable to check system health", "error")
            return None
    
    async def get_signal_types(self) -> dict:
        """Get available signal types (like frontend dropdown data)"""
        # For now, we'll use the known signal types since there's no API endpoint yet
        # In a real frontend, this would be a GET /api/v1/signal-types endpoint
        return {
            1: "FM - Frequency Modulation",
            2: "AM - Amplitude Modulation", 
            3: "SSB - Single Sideband",
            4: "CW - Continuous Wave",
            5: "PSK31 - Phase Shift Keying 31",
            6: "RTTY - Radio Teletype",
            7: "DMR - Digital Mobile Radio",
            8: "TETRA - Terrestrial Trunked Radio",
            9: "ACARS - Aircraft Communications",
            10: "ADS-B - Automatic Dependent Surveillance",
            11: "AIS - Automatic Identification System", 
            12: "UNKNOWN - Unknown Signal Type"
        }


async def simulate_frontend_workflow():
    """Simulate a complete frontend application workflow"""
    print("Frontend Application Simulator Starting...")
    print("=" * 60)
    
    app = FrontendApp()
    
    # Step 1: System Health Check (like app startup)
    print("\nStep 1: System Health Check")
    await app.check_system_health()
    
    # Step 2: User Authentication (like login page)
    print("\nStep 2: User Authentication")
    login_success = await app.login(
        certificate_cn="admin@radiomonitoring.local",
        certificate_serial="ABC123DEF456", 
        certificate_fingerprint="12:34:56:78:90:AB:CD:EF:12:34:56:78:90:AB:CD:EF:12:34:56:78"
    )
    
    if not login_success:
        print("[ERROR] Login failed - cannot continue with authenticated workflows")
        return
    
    # Step 3: Dashboard Data Loading (like main app page)
    print("\nStep 3: Dashboard Navigation")
    print("Current page:", app.app_state['current_page'])
    print("Authenticated user:", app.user_session['username'])
    
    # Step 4: Posts Management Workflow
    print("\nStep 4: Posts Management")
    posts = await app.load_posts_list()
    print(f"Found {len(posts)} posts in system")
    
    # Create a new post (like admin adding a post)
    timestamp = int(datetime.now(timezone.utc).timestamp())
    new_post_created = await app.create_new_post(
        name=f"Test Post {timestamp}",
        code=f"TEST_{timestamp}",
        description="Created by frontend simulator",
        region="TEST_REGION"
    )
    
    # Step 5: Observations Workflow
    if new_post_created and app.app_state['posts']:
        print("\nStep 5: Observations Management")
        test_post = app.app_state['posts'][0]  # Use the newly created post
        
        # Get available signal types (like populating a dropdown)
        signal_types = await app.get_signal_types()
        print(f"Available signal types: {len(signal_types)} options")
        
        # Load observations for this post
        await app.load_observations_list(post_id=test_post['id'])
        
        # Create a new observation (like user logging a signal)
        selected_signal_type = 1  # FM - Frequency Modulation
        signal_name = signal_types.get(selected_signal_type, "Unknown")
        print(f"Creating observation with signal type: {signal_name}")
        
        await app.create_observation(
            post_id=test_post['id'],
            frequency_min=146.0,
            frequency_max=147.0,
            signal_type_id=selected_signal_type,
            comment="Test observation from frontend simulator"
        )
    
    # Step 6: User Management (if user has permissions)
    print("\nStep 6: User Management")
    users = await app.load_users_list()
    print(f"Found {len(users)} users in system")
    
    # Step 7: Profile Management
    print("\nStep 7: User Profile")
    if 'user_profile' in app.app_state:
        profile = app.app_state['user_profile']
        print(f"Profile loaded: {profile.get('username')} (ID: {profile.get('id')})")
        print(f"Active: {profile.get('is_active')}, Last login: {profile.get('last_login_at')}")
    
    # Step 8: Session Management (like user logout)
    print("\nStep 8: Session Management")
    print("User sessions and logout simulation...")
    
    # Show final app state
    print("\nFinal Application State:")
    print(f"- Posts loaded: {len(app.app_state.get('posts', []))}")
    print(f"- Observations loaded: {len(app.app_state.get('observations', []))}")
    print(f"- Users loaded: {len(app.app_state.get('users', []))}")
    print(f"- Notifications: {len(app.app_state.get('notifications', []))}")
    
    # Logout (like user clicking logout)
    await app.logout()
    print("[SUCCESS] Frontend workflow simulation completed!")


async def simulate_error_scenarios():
    """Simulate error handling scenarios (like frontend error handling)"""
    print("\nTesting Error Scenarios...")
    
    app = FrontendApp()
    
    # Test 1: Invalid login
    print("\n1. Testing invalid login:")
    await app.login("invalid_user", "invalid_serial", "invalid_fingerprint")
    
    # Test 2: Unauthorized access
    print("\n2. Testing unauthorized access:")
    await app.load_users_list()
    
    # Test 3: Invalid data creation
    print("\n3. Testing invalid data:")
    await app.create_new_post("", "")  # Empty required fields


if __name__ == "__main__":
    print("Frontend Application Simulator")
    print("This simulates real frontend application workflows and user interactions")
    print("No pytest - just real application logic like a frontend would use")
    
    async def run_simulation():
        # Main workflow simulation
        await simulate_frontend_workflow()
        
        # Error handling simulation
        await simulate_error_scenarios()
        
        print("\n" + "=" * 60)
        print("Simulation Complete!")
        print("This demonstrates how a real frontend application would:")
        print("  - Handle user authentication and sessions")
        print("  - Load and display data from API endpoints") 
        print("  - Create new records through forms")
        print("  - Handle errors and show user feedback")
        print("  - Manage application state and navigation")
    
    # Run the simulation
    asyncio.run(run_simulation())