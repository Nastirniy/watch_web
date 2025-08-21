"""
Signal CRUD operations for observations, signal types, and categories.
Handles signal monitoring data and complex frequency-based queries.
"""

from datetime import datetime, date
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from loguru import logger
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.database.crud.base import CRUDBase
from backend.models.signal import (
    Observation, SignalType, Category, ModulationType,
    ObservationStatus, ConfidenceLevel
)
from backend.schemas.signal import ObservationCreate, ObservationUpdate


class CRUDObservation(CRUDBase[Observation, ObservationCreate, ObservationUpdate]):
    """CRUD operations for Observation model."""

    async def get_with_relations(self, db: AsyncSession, observation_id: int) -> Optional[Observation]:
        """Get observation with all related data loaded."""
        try:
            query = (
                select(Observation)
                .where(Observation.id == observation_id)
                .options(
                    selectinload(Observation.user),
                    selectinload(Observation.post),
                    selectinload(Observation.signal_type),
                    selectinload(Observation.modulation_type),
                    selectinload(Observation.media_files)
                )
            )
            result = await db.execute(query)
            return result.scalar_one_or_none()
        except Exception as e:
            logger.error(f"Error getting observation with relations for ID {observation_id}: {e}")
            raise

    async def get_by_frequency_range(
            self,
            db: AsyncSession,
            freq_min: Decimal,
            freq_max: Decimal,
            post_id: Optional[int] = None,
            active_only: bool = True
    ) -> List[Observation]:
        """Get observations within frequency range."""
        try:
            query = select(Observation).where(
                and_(
                    Observation.frequency_max >= freq_min,
                    Observation.frequency_min <= freq_max
                )
            )

            if post_id:
                query = query.where(Observation.post_id == post_id)

            if active_only:
                query = query.where(
                    and_(
                        Observation.status == ObservationStatus.ACTIVE,
                        Observation.is_deleted == False
                    )
                )

            result = await db.execute(query)
            return result.scalars().all()

        except Exception as e:
            logger.error(f"Error getting observations by frequency range {freq_min}-{freq_max}: {e}")
            raise

    async def get_by_time_range(
            self,
            db: AsyncSession,
            start_time: datetime,
            end_time: datetime,
            post_id: Optional[int] = None,
            signal_type_id: Optional[int] = None
    ) -> List[Observation]:
        """Get observations within time range."""
        try:
            query = select(Observation).where(
                and_(
                    Observation.observed_at >= start_time,
                    Observation.observed_at <= end_time
                )
            )

            if post_id:
                query = query.where(Observation.post_id == post_id)

            if signal_type_id:
                query = query.where(Observation.signal_type_id == signal_type_id)

            query = query.order_by(Observation.observed_at.desc())

            result = await db.execute(query)
            return result.scalars().all()

        except Exception as e:
            logger.error(f"Error getting observations by time range {start_time}-{end_time}: {e}")
            raise

    async def get_statistics(
            self,
            db: AsyncSession,
            start_date: Optional[date] = None,
            end_date: Optional[date] = None,
            post_id: Optional[int] = None
    ) -> Dict[str, int]:
        """Get observation statistics."""
        try:
            base_query = select(Observation)

            # Apply date filters
            if start_date:
                base_query = base_query.where(
                    func.date(Observation.observed_at) >= start_date
                )
            if end_date:
                base_query = base_query.where(
                    func.date(Observation.observed_at) <= end_date
                )
            if post_id:
                base_query = base_query.where(Observation.post_id == post_id)

            # Count by status
            stats = {}
            for status in ObservationStatus:
                query = base_query.where(Observation.status == status)
                count_query = select(func.count()).select_from(query.subquery())
                result = await db.execute(count_query)
                stats[status.value] = result.scalar()

            # Total count
            total_query = select(func.count()).select_from(base_query.subquery())
            result = await db.execute(total_query)
            stats['total'] = result.scalar()

            # Active (non-deleted) count
            active_query = base_query.where(Observation.is_deleted == False)
            count_query = select(func.count()).select_from(active_query.subquery())
            result = await db.execute(count_query)
            stats['active'] = result.scalar()

            return stats

        except Exception as e:
            logger.error(f"Error getting observation statistics: {e}")
            raise

    async def search_observations(
            self,
            db: AsyncSession,
            search_params: Dict[str, any],
            skip: int = 0,
            limit: int = 100
    ) -> Tuple[List[Observation], int]:
        """Complex search for observations with multiple criteria."""
        try:
            query = select(Observation)
            count_query = select(func.count()).select_from(Observation)

            filters = []

            # Frequency filters
            if 'frequency_min' in search_params and search_params['frequency_min']:
                filters.append(Observation.frequency_max >= search_params['frequency_min'])
            if 'frequency_max' in search_params and search_params['frequency_max']:
                filters.append(Observation.frequency_min <= search_params['frequency_max'])

            # Date range filters
            if 'date_from' in search_params and search_params['date_from']:
                filters.append(Observation.observed_at >= search_params['date_from'])
            if 'date_to' in search_params and search_params['date_to']:
                filters.append(Observation.observed_at <= search_params['date_to'])

            # Entity filters
            if 'post_ids' in search_params and search_params['post_ids']:
                filters.append(Observation.post_id.in_(search_params['post_ids']))
            if 'user_ids' in search_params and search_params['user_ids']:
                filters.append(Observation.user_id.in_(search_params['user_ids']))
            if 'signal_type_ids' in search_params and search_params['signal_type_ids']:
                filters.append(Observation.signal_type_id.in_(search_params['signal_type_ids']))

            # Status filters
            if 'statuses' in search_params and search_params['statuses']:
                filters.append(Observation.status.in_(search_params['statuses']))
            if 'confidence_levels' in search_params and search_params['confidence_levels']:
                filters.append(Observation.confidence_level.in_(search_params['confidence_levels']))

            # Signal strength filters
            if 'signal_strength_min' in search_params and search_params['signal_strength_min']:
                filters.append(Observation.signal_strength >= search_params['signal_strength_min'])
            if 'signal_strength_max' in search_params and search_params['signal_strength_max']:
                filters.append(Observation.signal_strength <= search_params['signal_strength_max'])

            # Text search in comments
            if 'comment_search' in search_params and search_params['comment_search']:
                filters.append(Observation.comment.like(f"%{search_params['comment_search']}%"))

            # Include/exclude deleted
            if not search_params.get('include_deleted', False):
                filters.append(Observation.is_deleted == False)

            # Apply all filters
            if filters:
                query = query.where(and_(*filters))
                count_query = count_query.where(and_(*filters))

            # Get total count
            total_result = await db.execute(count_query)
            total_count = total_result.scalar()

            # Apply ordering
            order_by = search_params.get('order_by', 'observed_at')
            order_direction = search_params.get('order_direction', 'desc')

            if hasattr(Observation, order_by):
                column = getattr(Observation, order_by)
                if order_direction.lower() == 'desc':
                    query = query.order_by(column.desc())
                else:
                    query = query.order_by(column.asc())

            # Apply pagination
            query = query.offset(skip).limit(limit)

            # Load relationships
            query = query.options(
                selectinload(Observation.user),
                selectinload(Observation.post),
                selectinload(Observation.signal_type)
            )

            result = await db.execute(query)
            observations = result.scalars().all()

            return observations, total_count

        except Exception as e:
            logger.error(f"Error searching observations: {e}")
            raise

    async def mark_disappeared(
            self,
            db: AsyncSession,
            observation_id: int,
            user_id: int,
            comment: Optional[str] = None
    ) -> Optional[Observation]:
        """Mark observation as disappeared."""
        try:
            observation = await self.get(db, observation_id)
            if observation:
                observation.mark_disappeared(user_id, comment)
                await db.commit()
                await db.refresh(observation)
                logger.info(f"Marked observation {observation_id} as disappeared by user {user_id}")
                return observation
            return None
        except Exception as e:
            await db.rollback()
            logger.error(f"Error marking observation {observation_id} as disappeared: {e}")
            raise

    async def bulk_update_status(
            self,
            db: AsyncSession,
            observation_ids: List[int],
            new_status: ObservationStatus,
            user_id: Optional[int] = None
    ) -> int:
        """Bulk update observation status."""
        try:
            from sqlalchemy import update

            update_data = {"status": new_status}
            if new_status == ObservationStatus.DISAPPEARED:
                update_data.update({
                    "disappeared_at": datetime.utcnow(),
                    "disappeared_by": user_id
                })

            query = (
                update(Observation)
                .where(Observation.id.in_(observation_ids))
                .values(**update_data)
            )

            result = await db.execute(query)
            await db.commit()

            count = result.rowcount
            logger.info(f"Bulk updated {count} observations to status {new_status.value}")
            return count

        except Exception as e:
            await db.rollback()
            logger.error(f"Error bulk updating observation status: {e}")
            raise


class CRUDSignalType(CRUDBase[SignalType, dict, dict]):
    """CRUD operations for SignalType model."""

    async def get_by_code(self, db: AsyncSession, code: str) -> Optional[SignalType]:
        """Get signal type by code."""
        try:
            query = select(SignalType).where(SignalType.code == code)
            result = await db.execute(query)
            return result.scalar_one_or_none()
        except Exception as e:
            logger.error(f"Error getting signal type by code {code}: {e}")
            raise

    async def get_active_types(self, db: AsyncSession) -> List[SignalType]:
        """Get all active signal types ordered by sort_order."""
        try:
            query = (
                select(SignalType)
                .where(SignalType.is_active == True)
                .order_by(SignalType.sort_order, SignalType.name)
                .options(selectinload(SignalType.category))
            )
            result = await db.execute(query)
            return result.scalars().all()
        except Exception as e:
            logger.error(f"Error getting active signal types: {e}")
            raise

    async def get_by_category(self, db: AsyncSession, category_id: int) -> List[SignalType]:
        """Get signal types by category."""
        try:
            query = (
                select(SignalType)
                .where(SignalType.category_id == category_id)
                .order_by(SignalType.sort_order, SignalType.name)
            )
            result = await db.execute(query)
            return result.scalars().all()
        except Exception as e:
            logger.error(f"Error getting signal types by category {category_id}: {e}")
            raise


class CRUDCategory(CRUDBase[Category, dict, dict]):
    """CRUD operations for Category model."""

    async def get_by_name(self, db: AsyncSession, name: str) -> Optional[Category]:
        """Get category by name."""
        try:
            query = select(Category).where(Category.name == name)
            result = await db.execute(query)
            return result.scalar_one_or_none()
        except Exception as e:
            logger.error(f"Error getting category by name {name}: {e}")
            raise

    async def get_with_signal_types(self, db: AsyncSession, category_id: int) -> Optional[Category]:
        """Get category with signal types loaded."""
        try:
            query = (
                select(Category)
                .where(Category.id == category_id)
                .options(selectinload(Category.signal_types))
            )
            result = await db.execute(query)
            return result.scalar_one_or_none()
        except Exception as e:
            logger.error(f"Error getting category with signal types for ID {category_id}: {e}")
            raise


class CRUDModulationType(CRUDBase[ModulationType, dict, dict]):
    """CRUD operations for ModulationType model."""

    async def get_by_name(self, db: AsyncSession, name: str) -> Optional[ModulationType]:
        """Get modulation type by name."""
        try:
            query = select(ModulationType).where(ModulationType.name == name)
            result = await db.execute(query)
            return result.scalar_one_or_none()
        except Exception as e:
            logger.error(f"Error getting modulation type by name {name}: {e}")
            raise


# Create instances
observation = CRUDObservation(Observation)
signal_type = CRUDSignalType(SignalType)
category = CRUDCategory(Category)
modulation_type = CRUDModulationType(ModulationType)
