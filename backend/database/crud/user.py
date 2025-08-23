"""
User CRUD operations with authentication and role management.
Handles user creation, authentication, PKI certificates, and RBAC.
"""

from datetime import datetime, timedelta
from typing import List, Optional

from loguru import logger
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.database.crud.base import CRUDBase
from backend.database.models import User, Role, Permission, UserSession, UserRole, RolePermission
from backend.api.schemas import UserCreate, UserUpdate


class CRUDUser(CRUDBase[User, UserCreate, UserUpdate]):
    """CRUD operations for User model."""

    async def get_by_username(self, db: AsyncSession, username: str) -> Optional[User]:
        """Get user by username."""
        try:
            query = select(User).where(User.username == username)
            result = await db.execute(query)
            return result.scalar_one_or_none()
        except Exception as e:
            logger.error(f"Error getting user by username {username}: {e}")
            raise

    async def get_by_certificate_cn(self, db: AsyncSession, certificate_cn: str) -> Optional[User]:
        """Get user by PKI certificate Common Name."""
        try:
            query = select(User).where(User.pki_certificate_cn == certificate_cn)
            result = await db.execute(query)
            return result.scalar_one_or_none()
        except Exception as e:
            logger.error(f"Error getting user by certificate CN {certificate_cn}: {e}")
            raise

    async def get_by_certificate_serial(self, db: AsyncSession, certificate_serial: str) -> Optional[User]:
        """Get user by PKI certificate serial number."""
        try:
            query = select(User).where(User.pki_certificate_serial == certificate_serial)
            result = await db.execute(query)
            return result.scalar_one_or_none()
        except Exception as e:
            logger.error(f"Error getting user by certificate serial {certificate_serial}: {e}")
            raise

    async def get_with_roles(self, db: AsyncSession, user_id: int) -> Optional[User]:
        """Get user with roles and permissions loaded."""
        try:
            query = (
                select(User)
                .where(User.id == user_id)
                .options(
                    selectinload(User.roles).selectinload(Role.permissions)
                )
            )
            result = await db.execute(query)
            return result.scalar_one_or_none()
        except Exception as e:
            logger.error(f"Error getting user with roles for ID {user_id}: {e}")
            raise

    async def create_with_certificate(
            self,
            db: AsyncSession,
            user_data: UserCreate,
            certificate_cn: str,
            certificate_serial: str,
            certificate_fingerprint: str
    ) -> User:
        """Create user with PKI certificate information."""
        try:
            # Check if certificate is already registered
            existing_user = await self.get_by_certificate_serial(db, certificate_serial)
            if existing_user:
                raise ValueError(f"Certificate with serial {certificate_serial} already registered")

            # Create user data with certificate info
            user_dict = user_data.model_dump()
            user_dict.update({
                "pki_certificate_cn": certificate_cn,
                "pki_certificate_serial": certificate_serial,
                "pki_certificate_fingerprint": certificate_fingerprint,
            })

            db_user = User(**user_dict)
            db.add(db_user)
            await db.commit()
            await db.refresh(db_user)

            logger.info(f"Created user {db_user.username} with certificate CN: {certificate_cn}")
            return db_user

        except Exception as e:
            await db.rollback()
            logger.error(f"Error creating user with certificate: {e}")
            raise

    async def update_last_login(self, db: AsyncSession, user_id: int) -> None:
        """Update user's last login timestamp."""
        try:
            user = await self.get(db, user_id)
            if user:
                user.last_login_at = datetime.utcnow()
                await db.commit()
        except Exception as e:
            logger.error(f"Error updating last login for user {user_id}: {e}")
            raise

    async def assign_role(self, db: AsyncSession, user_id: int, role_id: int,
                          assigned_by: Optional[int] = None) -> bool:
        """Assign role to user."""
        try:
            # Check if assignment already exists
            query = select(UserRole).where(
                and_(UserRole.user_id == user_id, UserRole.role_id == role_id)
            )
            result = await db.execute(query)
            existing = result.scalar_one_or_none()

            if existing:
                return False  # Already assigned

            # Create new assignment
            user_role = UserRole(
                user_id=user_id,
                role_id=role_id,
                assigned_by=assigned_by,
                assigned_at=datetime.utcnow()
            )
            db.add(user_role)
            await db.commit()

            logger.info(f"Assigned role {role_id} to user {user_id}")
            return True

        except Exception as e:
            await db.rollback()
            logger.error(f"Error assigning role {role_id} to user {user_id}: {e}")
            raise

    async def remove_role(self, db: AsyncSession, user_id: int, role_id: int) -> bool:
        """Remove role from user."""
        try:
            query = select(UserRole).where(
                and_(UserRole.user_id == user_id, UserRole.role_id == role_id)
            )
            result = await db.execute(query)
            user_role = result.scalar_one_or_none()

            if user_role:
                await db.delete(user_role)
                await db.commit()
                logger.info(f"Removed role {role_id} from user {user_id}")
                return True

            return False  # Role not assigned

        except Exception as e:
            await db.rollback()
            logger.error(f"Error removing role {role_id} from user {user_id}: {e}")
            raise

    async def get_user_permissions(self, db: AsyncSession, user_id: int) -> List[str]:
        """Get all permissions for a user."""
        try:
            user = await self.get_with_roles(db, user_id)
            if not user:
                return []

            if user.is_superuser:
                return ["*"]  # Superuser has all permissions

            permissions = set()
            for role in user.roles:
                for permission in role.permissions:
                    permissions.add(permission.name)

            return list(permissions)

        except Exception as e:
            logger.error(f"Error getting permissions for user {user_id}: {e}")
            raise

    async def has_permission(self, db: AsyncSession, user_id: int, permission_name: str) -> bool:
        """Check if user has specific permission."""
        try:
            user = await self.get_with_roles(db, user_id)
            if not user:
                return False

            if user.is_superuser:
                return True

            for role in user.roles:
                for permission in role.permissions:
                    if permission.name == permission_name:
                        return True

            return False

        except Exception as e:
            logger.error(f"Error checking permission {permission_name} for user {user_id}: {e}")
            raise


class CRUDRole(CRUDBase[Role, dict, dict]):
    """CRUD operations for Role model."""

    async def get_by_name(self, db: AsyncSession, name: str) -> Optional[Role]:
        """Get role by name."""
        try:
            query = select(Role).where(Role.name == name)
            result = await db.execute(query)
            return result.scalar_one_or_none()
        except Exception as e:
            logger.error(f"Error getting role by name {name}: {e}")
            raise

    async def get_with_permissions(self, db: AsyncSession, role_id: int) -> Optional[Role]:
        """Get role with permissions loaded."""
        try:
            query = (
                select(Role)
                .where(Role.id == role_id)
                .options(selectinload(Role.permissions))
            )
            result = await db.execute(query)
            return result.scalar_one_or_none()
        except Exception as e:
            logger.error(f"Error getting role with permissions for ID {role_id}: {e}")
            raise

    async def assign_permission(
            self,
            db: AsyncSession,
            role_id: int,
            permission_id: int,
            granted_by: Optional[int] = None
    ) -> bool:
        """Assign permission to role."""
        try:
            # Check if assignment already exists
            query = select(RolePermission).where(
                and_(RolePermission.role_id == role_id, RolePermission.permission_id == permission_id)
            )
            result = await db.execute(query)
            existing = result.scalar_one_or_none()

            if existing:
                return False

            # Create new assignment
            role_permission = RolePermission(
                role_id=role_id,
                permission_id=permission_id,
                granted_by=granted_by,
                granted_at=datetime.utcnow()
            )
            db.add(role_permission)
            await db.commit()

            logger.info(f"Assigned permission {permission_id} to role {role_id}")
            return True

        except Exception as e:
            await db.rollback()
            logger.error(f"Error assigning permission {permission_id} to role {role_id}: {e}")
            raise

    async def remove_permission(self, db: AsyncSession, role_id: int, permission_id: int) -> bool:
        """Remove permission from role."""
        try:
            query = select(RolePermission).where(
                and_(RolePermission.role_id == role_id, RolePermission.permission_id == permission_id)
            )
            result = await db.execute(query)
            role_permission = result.scalar_one_or_none()

            if role_permission:
                await db.delete(role_permission)
                await db.commit()
                logger.info(f"Removed permission {permission_id} from role {role_id}")
                return True

            return False

        except Exception as e:
            await db.rollback()
            logger.error(f"Error removing permission {permission_id} from role {role_id}: {e}")
            raise


class CRUDPermission(CRUDBase[Permission, dict, dict]):
    """CRUD operations for Permission model."""

    async def get_by_name(self, db: AsyncSession, name: str) -> Optional[Permission]:
        """Get permission by name."""
        try:
            query = select(Permission).where(Permission.name == name)
            result = await db.execute(query)
            return result.scalar_one_or_none()
        except Exception as e:
            logger.error(f"Error getting permission by name {name}: {e}")
            raise

    async def get_by_resource_action(
            self,
            db: AsyncSession,
            resource: str,
            action: str
    ) -> Optional[Permission]:
        """Get permission by resource and action."""
        try:
            query = select(Permission).where(
                and_(Permission.resource == resource, Permission.action == action)
            )
            result = await db.execute(query)
            return result.scalar_one_or_none()
        except Exception as e:
            logger.error(f"Error getting permission for {resource}:{action}: {e}")
            raise


class CRUDUserSession(CRUDBase[UserSession, dict, dict]):
    """CRUD operations for UserSession model."""

    async def create_session(
            self,
            db: AsyncSession,
            user_id: int,
            session_id: str,
            ip_address: Optional[str] = None,
            user_agent: Optional[str] = None,
            expire_minutes: int = 30
    ) -> UserSession:
        """Create new user session."""
        try:
            expires_at = datetime.utcnow() + timedelta(minutes=expire_minutes)

            session = UserSession(
                id=session_id,
                user_id=user_id,
                ip_address=ip_address,
                user_agent=user_agent,
                expires_at=expires_at,
            )

            db.add(session)
            await db.commit()
            await db.refresh(session)

            logger.debug(f"Created session {session_id} for user {user_id}")
            return session

        except Exception as e:
            await db.rollback()
            logger.error(f"Error creating session for user {user_id}: {e}")
            raise

    async def get_active_session(self, db: AsyncSession, session_id: str) -> Optional[UserSession]:
        """Get active session by ID."""
        try:
            query = (
                select(UserSession)
                .where(
                    and_(
                        UserSession.id == session_id,
                        UserSession.is_active == True,
                        UserSession.expires_at > datetime.utcnow()
                    )
                )
                .options(selectinload(UserSession.user))
            )
            result = await db.execute(query)
            return result.scalar_one_or_none()
        except Exception as e:
            logger.error(f"Error getting active session {session_id}: {e}")
            raise

    async def refresh_session(self, db: AsyncSession, session_id: str, extend_minutes: int = 30) -> bool:
        """Refresh session expiration time."""
        try:
            session = await self.get_active_session(db, session_id)
            if session:
                session.refresh(extend_minutes)
                await db.commit()
                return True
            return False
        except Exception as e:
            logger.error(f"Error refreshing session {session_id}: {e}")
            raise

    async def invalidate_session(self, db: AsyncSession, session_id: str, reason: str = "logout") -> bool:
        """Invalidate user session."""
        try:
            session = await self.get(db, session_id)
            if session:
                session.is_active = False
                session.logout_reason = reason
                await db.commit()
                logger.debug(f"Invalidated session {session_id}: {reason}")
                return True
            return False
        except Exception as e:
            logger.error(f"Error invalidating session {session_id}: {e}")
            raise

    async def cleanup_expired_sessions(self, db: AsyncSession) -> int:
        """Clean up expired sessions."""
        try:
            from sqlalchemy import update

            # Mark expired sessions as inactive
            query = (
                update(UserSession)
                .where(
                    and_(
                        UserSession.expires_at <= datetime.utcnow(),
                        UserSession.is_active == True
                    )
                )
                .values(is_active=False, logout_reason="expired")
            )

            result = await db.execute(query)
            await db.commit()

            count = result.rowcount
            if count > 0:
                logger.info(f"Cleaned up {count} expired sessions")

            return count

        except Exception as e:
            logger.error(f"Error cleaning up expired sessions: {e}")
            raise


# Create instances
user = CRUDUser(User)
role = CRUDRole(Role)
permission = CRUDPermission(Permission)
user_session = CRUDUserSession(UserSession)
