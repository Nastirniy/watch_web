"""
Post CRUD operations for monitoring stations/locations.
Handles post management, connection status, and location-based queries.
"""

from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from loguru import logger
from sqlalchemy import and_, func, select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.crud.base import CRUDBase
from backend.database.models import Post, ConnectionStatus
from backend.api.schemas.post import PostCreate, PostUpdate


class CRUDPost(CRUDBase[Post, PostCreate, PostUpdate]):
    """CRUD operations for Post model."""

    async def get_by_code(self, db: AsyncSession, code: str) -> Optional[Post]:
        """Get post by unique code."""
        try:
            query = select(Post).where(Post.code == code)
            result = await db.execute(query)
            return result.scalar_one_or_none()
        except Exception as e:
            logger.error(f"Error getting post by code {code}: {e}")
            raise

    async def get_active_posts(self, db: AsyncSession) -> List[Post]:
        """Get all active posts."""
        try:
            query = (
                select(Post)
                .where(Post.is_active == True)
                .order_by(Post.name)
            )
            result = await db.execute(query)
            return result.scalars().all()
        except Exception as e:
            logger.error(f"Error getting active posts: {e}")
            raise

    async def get_by_region(self, db: AsyncSession, region: str) -> List[Post]:
        """Get posts by region."""
        try:
            query = (
                select(Post)
                .where(Post.region == region)
                .order_by(Post.name)
            )
            result = await db.execute(query)
            return result.scalars().all()
        except Exception as e:
            logger.error(f"Error getting posts by region {region}: {e}")
            raise

    async def get_by_connection_status(
            self,
            db: AsyncSession,
            status: ConnectionStatus
    ) -> List[Post]:
        """Get posts by connection status."""
        try:
            query = (
                select(Post)
                .where(Post.connection_status == status)
                .order_by(Post.last_connection_at.desc())
            )
            result = await db.execute(query)
            return result.scalars().all()
        except Exception as e:
            logger.error(f"Error getting posts by status {status}: {e}")
            raise

    async def get_online_posts(self, db: AsyncSession) -> List[Post]:
        """Get all online posts."""
        return await self.get_by_connection_status(db, ConnectionStatus.ONLINE)

    async def get_offline_posts(self, db: AsyncSession) -> List[Post]:
        """Get all offline posts."""
        return await self.get_by_connection_status(db, ConnectionStatus.OFFLINE)

    async def get_posts_with_recent_activity(
            self,
            db: AsyncSession,
            hours: int = 24
    ) -> List[Post]:
        """Get posts with recent connection activity."""
        try:
            cutoff_time = datetime.utcnow() - timedelta(hours=hours)
            query = (
                select(Post)
                .where(
                    and_(
                        Post.last_connection_at >= cutoff_time,
                        Post.is_active == True
                    )
                )
                .order_by(Post.last_connection_at.desc())
            )
            result = await db.execute(query)
            return result.scalars().all()
        except Exception as e:
            logger.error(f"Error getting posts with recent activity: {e}")
            raise

    async def get_posts_by_location(
            self,
            db: AsyncSession,
            latitude: float,
            longitude: float,
            radius_km: float
    ) -> List[Post]:
        """Get posts within radius of given coordinates."""
        try:
            # Using Haversine formula for distance calculation
            # Note: For production, consider using PostGIS or similar spatial extension
            query = select(Post).where(
                and_(
                    Post.latitude.isnot(None),
                    Post.longitude.isnot(None),
                    Post.is_active == True
                )
            )

            result = await db.execute(query)
            all_posts = result.scalars().all()

            # Filter by distance (simple implementation)
            nearby_posts = []
            for post in all_posts:
                if post.latitude and post.longitude:
                    distance = self._calculate_distance(
                        latitude, longitude,
                        float(post.latitude), float(post.longitude)
                    )
                    if distance <= radius_km:
                        nearby_posts.append(post)

            return nearby_posts

        except Exception as e:
            logger.error(f"Error getting posts by location: {e}")
            raise

    def _calculate_distance(self, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Calculate distance between two coordinates using Haversine formula."""
        import math

        # Convert latitude and longitude from degrees to radians
        lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])

        # Haversine formula
        dlat = lat2 - lat1
        dlon = lon2 - lon1
        a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
        c = 2 * math.asin(math.sqrt(a))
        r = 6371  # Radius of Earth in kilometers

        return c * r

    async def update_connection_status(
            self,
            db: AsyncSession,
            post_id: int,
            status: ConnectionStatus
    ) -> Optional[Post]:
        """Update post connection status."""
        try:
            post = await self.get(db, post_id)
            if post:
                post.update_connection_status(status)
                await db.commit()
                await db.refresh(post)
                logger.info(f"Updated post {post_id} status to {status.value}")
                return post
            return None
        except Exception as e:
            await db.rollback()
            logger.error(f"Error updating post {post_id} status: {e}")
            raise

    async def bulk_update_offline_status(
            self,
            db: AsyncSession,
            timeout_minutes: int = 60
    ) -> int:
        """Mark posts as offline if no recent connection."""
        try:
            from sqlalchemy import update

            cutoff_time = datetime.utcnow() - timedelta(minutes=timeout_minutes)

            query = (
                update(Post)
                .where(
                    and_(
                        Post.last_connection_at < cutoff_time,
                        Post.connection_status == ConnectionStatus.ONLINE
                    )
                )
                .values(connection_status=ConnectionStatus.OFFLINE)
            )

            result = await db.execute(query)
            await db.commit()

            count = result.rowcount
            if count > 0:
                logger.info(f"Marked {count} posts as offline due to timeout")

            return count

        except Exception as e:
            await db.rollback()
            logger.error(f"Error bulk updating offline status: {e}")
            raise

    async def get_connection_statistics(self, db: AsyncSession) -> Dict[str, int]:
        """Get connection status statistics."""
        try:
            stats = {}

            # Count by status
            for status in ConnectionStatus:
                query = select(func.count()).where(Post.connection_status == status)
                result = await db.execute(query)
                stats[status.value] = result.scalar()

            # Total active posts
            query = select(func.count()).where(Post.is_active == True)
            result = await db.execute(query)
            stats['total_active'] = result.scalar()

            # Total posts
            query = select(func.count()).select_from(Post)
            result = await db.execute(query)
            stats['total'] = result.scalar()

            return stats

        except Exception as e:
            logger.error(f"Error getting connection statistics: {e}")
            raise

    async def get_with_observations_count(
            self,
            db: AsyncSession,
            post_id: int,
            days: int = 30
    ) -> Optional[Tuple[Post, int]]:
        """Get post with observation count for specified period."""
        try:
            post = await self.get(db, post_id)
            if not post:
                return None

            # Count observations in the last N days
            cutoff_date = datetime.utcnow() - timedelta(days=days)

            from backend.database.models.signal import Observation
            query = (
                select(func.count())
                .where(
                    and_(
                        Observation.post_id == post_id,
                        Observation.observed_at >= cutoff_date,
                        Observation.is_deleted == False
                    )
                )
            )

            result = await db.execute(query)
            count = result.scalar()

            return post, count

        except Exception as e:
            logger.error(f"Error getting post with observation count: {e}")
            raise

    async def search_posts(
            self,
            db: AsyncSession,
            search_term: str,
            region: Optional[str] = None,
            status: Optional[ConnectionStatus] = None,
            active_only: bool = True
    ) -> List[Post]:
        """Search posts by name, code, or description."""
        try:
            filters = []

            # Text search
            if search_term:
                text_filter = or_(
                    Post.name.ilike(f"%{search_term}%"),
                    Post.code.ilike(f"%{search_term}%"),
                    Post.description.ilike(f"%{search_term}%")
                )
                filters.append(text_filter)

            # Region filter
            if region:
                filters.append(Post.region == region)

            # Status filter
            if status:
                filters.append(Post.connection_status == status)

            # Active filter
            if active_only:
                filters.append(Post.is_active == True)

            query = select(Post)
            if filters:
                query = query.where(and_(*filters))

            query = query.order_by(Post.name)

            result = await db.execute(query)
            return result.scalars().all()

        except Exception as e:
            logger.error(f"Error searching posts: {e}")
            raise


# Create instance
post = CRUDPost(Post)
