"""Test configuration and fixtures"""
import pytest
import pytest_asyncio
from typing import AsyncGenerator
import asyncio
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, create_engine
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import NullPool
from httpx import AsyncClient
import os
from datetime import datetime, timezone

# Import application components
from app.main import fastapi_app
from app.backend.database import get_session
from app.backend.config import settings
from app.backend.models.user import User, UserSession
from app.backend.models.role import Role, Permission, RolePermission, UserRole
from app.backend.models.post import Post
from app.backend.models.observation import SignalType, Category, ModulationType
from app.backend.enums.general import Permissions
from app.backend.utils.pki_generator import PKICertificateGenerator
from app.backend.services.auth import AuthService


# Test MariaDB URL - use config from config.toml with test database
TEST_DATABASE_URL = f"mysql+aiomysql://{settings.database.user}:{settings.database.password}@{settings.database.host}:{settings.database.port}/radio_monitoring_test"


@pytest.fixture(scope="session")
def event_loop():
    """Create event loop for async tests"""
    policy = asyncio.get_event_loop_policy()
    loop = policy.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="session")
async def test_engine():
    """Create test database engine for MariaDB"""
    from sqlalchemy import text
    
    # First connect without database to create test database
    engine_without_db = create_async_engine(
        f"mysql+aiomysql://{settings.database.user}:{settings.database.password}@{settings.database.host}:{settings.database.port}/",
        echo=False,
        connect_args={}
    )
    
    async with engine_without_db.begin() as conn:
        # Drop and recreate test database
        await conn.execute(text("DROP DATABASE IF EXISTS radio_monitoring_test"))
        await conn.execute(text("CREATE DATABASE radio_monitoring_test CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"))
    
    await engine_without_db.dispose()
    
    # Now connect to test database
    engine = create_async_engine(
        TEST_DATABASE_URL,
        echo=False,
        poolclass=NullPool,
        connect_args={
            "connect_timeout": 10,
            "charset": "utf8mb4",
            "use_unicode": True,
            "autocommit": False,
        }
    )
    
    # Create all tables
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    
    yield engine
    
    # Cleanup - drop test database
    await engine.dispose()
    
    cleanup_engine = create_async_engine(
        f"mysql+aiomysql://{settings.database.user}:{settings.database.password}@{settings.database.host}:{settings.database.port}/",
        echo=False,
        connect_args={}
    )
    async with cleanup_engine.begin() as conn:
        await conn.execute(text("DROP DATABASE IF EXISTS radio_monitoring_test"))
    await cleanup_engine.dispose()


@pytest_asyncio.fixture(scope="function")
async def test_session(test_engine) -> AsyncGenerator[AsyncSession, None]:
    """Create test database session with transaction rollback"""
    from sqlalchemy import text
    
    async_session_maker = async_sessionmaker(
        test_engine,
        class_=AsyncSession,
        expire_on_commit=False
    )
    
    async with async_session_maker() as session:
        try:
            yield session
        finally:
            # Clean up all tables for fresh start
            try:
                await session.rollback()
                
                # Clear all tables in reverse order to handle foreign keys
                await session.exec(text("SET FOREIGN_KEY_CHECKS = 0"))
                await session.exec(text("TRUNCATE TABLE user_role"))
                await session.exec(text("TRUNCATE TABLE role_permission"))  
                await session.exec(text("TRUNCATE TABLE user_session"))
                await session.exec(text("TRUNCATE TABLE audit"))
                await session.exec(text("TRUNCATE TABLE observation"))
                await session.exec(text("TRUNCATE TABLE signal_type"))
                await session.exec(text("TRUNCATE TABLE category"))
                await session.exec(text("TRUNCATE TABLE modulation_type"))
                await session.exec(text("TRUNCATE TABLE post"))
                await session.exec(text("TRUNCATE TABLE user"))
                await session.exec(text("TRUNCATE TABLE role"))
                await session.exec(text("TRUNCATE TABLE permission"))
                await session.exec(text("SET FOREIGN_KEY_CHECKS = 1"))
                
                await session.commit()
            except Exception:
                pass
            
            try:
                await session.close()
            except Exception:
                pass


@pytest_asyncio.fixture(scope="function") 
async def client(test_session):
    """Create test client with overridden database session and disabled rate limiting"""
    
    async def get_test_session():
        yield test_session
    
    fastapi_app.dependency_overrides[get_session] = get_test_session
    
    # Disable rate limiting for tests by clearing the rate limit storage and making the check always return True
    from app.backend.middleware.auth import SecurityMiddleware
    original_check_rate_limit = SecurityMiddleware._check_rate_limit
    SecurityMiddleware.rate_limit_storage = {}  # Clear any existing rate limits
    
    async def mock_rate_limit_check(self, request):
        return True  # Always allow requests during tests
    
    SecurityMiddleware._check_rate_limit = mock_rate_limit_check
    
    try:
        from httpx import ASGITransport
        async with AsyncClient(transport=ASGITransport(app=fastapi_app), base_url="http://test") as ac:
            yield ac
    finally:
        # Restore original rate limiting behavior
        SecurityMiddleware._check_rate_limit = original_check_rate_limit
        SecurityMiddleware.rate_limit_storage = {}
        # Clean up override
        fastapi_app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def admin_user(test_session) -> User:
    """Create admin user for tests"""
    # Generate unique PKI credentials for this test run
    import time
    unique_serial = int(time.time() * 1000) % (10**8)  # Use timestamp for uniqueness
    unique_username = f"admin_{unique_serial}"
    admin_pki = PKICertificateGenerator.generate_test_certificate(unique_username, unique_serial)
    
    # Check if admin role already exists
    from sqlmodel import select
    result = await test_session.exec(select(Role).where(Role.name == "admin"))
    admin_role = result.first()
    
    if not admin_role:
        # Create admin role
        admin_role = Role(
            name="admin",
            display_name="Administrator",
            description="Full system access"
        )
        test_session.add(admin_role)
        await test_session.flush()
        
        # Create all permissions
        permissions = []
        for perm in Permissions:
            # Check if permission already exists
            perm_result = await test_session.exec(select(Permission).where(Permission.name == perm.value))
            permission = perm_result.first()
            
            if not permission:
                permission = Permission(name=perm.value)
                test_session.add(permission)
                await test_session.flush()
            
            permissions.append(permission)
            
            # Check if role permission already exists
            role_perm_result = await test_session.exec(
                select(RolePermission).where(
                    RolePermission.role_id == admin_role.id,
                    RolePermission.permission_id == permission.id
                )
            )
            if not role_perm_result.first():
                # Assign to admin role
                role_perm = RolePermission(
                    role_id=admin_role.id,
                    permission_id=permission.id
                )
                test_session.add(role_perm)
    
    # Create admin user with unique credentials (hashed)
    admin_user = User(
        username=f"admin_{unique_serial}",  # Make username unique too
        username_alias="Test Administrator",
        pki_certificate_cn_hash=AuthService.hash_pki_data(admin_pki["cn"]),
        pki_certificate_serial_hash=AuthService.hash_pki_data(admin_pki["serial"]),
        pki_certificate_fingerprint_hash=AuthService.hash_pki_data(admin_pki["fingerprint"]),
        is_active=True
    )
    test_session.add(admin_user)
    await test_session.flush()
    
    # Assign admin role to user
    user_role = UserRole(
        user_id=admin_user.id,
        role_id=admin_role.id
    )
    test_session.add(user_role)
    
    await test_session.commit()
    return admin_user


@pytest_asyncio.fixture
async def regular_user(test_session) -> User:
    """Create regular user for tests"""
    # Generate unique PKI credentials
    import time
    unique_serial = int(time.time() * 1000) % (10**8) + 1000  # Add offset to avoid admin collision
    unique_username = f"testuser_{unique_serial}"
    user_pki = PKICertificateGenerator.generate_test_certificate(unique_username, unique_serial)
    
    # Create regular user (hashed)
    user = User(
        username=f"testuser_{unique_serial}",
        username_alias="Test User",
        pki_certificate_cn_hash=AuthService.hash_pki_data(user_pki["cn"]),
        pki_certificate_serial_hash=AuthService.hash_pki_data(user_pki["serial"]),
        pki_certificate_fingerprint_hash=AuthService.hash_pki_data(user_pki["fingerprint"]),
        is_active=True
    )
    test_session.add(user)
    await test_session.commit()
    return user


@pytest_asyncio.fixture
async def test_post(test_session) -> Post:
    """Create test post"""
    import time
    unique_code = f"TEST{int(time.time() * 1000) % (10**6)}"
    post = Post(
        name="Test Post",
        code=unique_code,
        description="Test monitoring post",
        region="Test Region"
    )
    test_session.add(post)
    await test_session.commit()
    return post


@pytest_asyncio.fixture  
async def test_signal_type(test_session) -> SignalType:
    """Create test signal type with category"""
    import time
    unique_suffix = int(time.time() * 1000) % (10**6)
    
    # Create category first
    category = Category(
        name=f"Test Category {unique_suffix}",
        alias=f"TEST_CAT_{unique_suffix}"
    )
    test_session.add(category)
    await test_session.flush()
    
    # Create signal type
    signal_type = SignalType(
        code=f"TEST_SIG_{unique_suffix}",
        name=f"Test Signal Type {unique_suffix}",
        category_id=category.id,
        description="Test signal for testing"
    )
    test_session.add(signal_type)
    await test_session.commit()
    return signal_type


@pytest_asyncio.fixture
async def admin_auth_headers(admin_user, test_session) -> dict:
    """Generate authentication headers for admin user with valid session"""
    # Create a valid user session
    import secrets
    from datetime import timedelta
    session_id = secrets.token_urlsafe(64)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=8)
    
    user_session = UserSession(
        id=session_id,
        user_id=admin_user.id,
        ip_address="127.0.0.1",
        user_agent="test-client",
        expires_at=expires_at,
        is_active=True
    )
    test_session.add(user_session)
    await test_session.commit()
    
    token = AuthService.create_access_token(admin_user.id)
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def user_auth_headers(regular_user, test_session) -> dict:
    """Generate authentication headers for regular user with valid session"""  
    # Create a valid user session
    import secrets
    from datetime import timedelta
    session_id = secrets.token_urlsafe(64)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=8)
    
    user_session = UserSession(
        id=session_id,
        user_id=regular_user.id,
        ip_address="127.0.0.1",
        user_agent="test-client",
        expires_at=expires_at,
        is_active=True
    )
    test_session.add(user_session)
    await test_session.commit()
    
    token = AuthService.create_access_token(regular_user.id)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def pki_credentials():
    """Generate test PKI credentials"""
    import time
    unique_serial = int(time.time() * 1000) % (10**8) + 2000
    return PKICertificateGenerator.generate_test_certificate("testcert", unique_serial)


@pytest.fixture
def sample_login_data():
    """Sample login data for authentication tests"""
    import time
    unique_serial = int(time.time() * 1000) % (10**8) + 3000
    pki = PKICertificateGenerator.generate_test_certificate("logintest", unique_serial)
    return {
        "certificate_cn": pki["cn"],
        "certificate_serial": pki["serial"],
        "certificate_fingerprint": pki["fingerprint"]
    }