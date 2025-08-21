"""
Base CRUD operations with async database support.
Provides generic CRUD functionality for all models.
"""

from typing import Any, Dict, Generic, List, Optional, Type, TypeVar, Union

from fastapi.encoders import jsonable_encoder
from loguru import logger
from sqlalchemy import and_, func, or_, select, update, delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlmodel import SQLModel

ModelType = TypeVar("ModelType", bound=SQLModel)
CreateSchemaType = TypeVar("CreateSchemaType", bound=SQLModel)
UpdateSchemaType = TypeVar("UpdateSchemaType", bound=SQLModel)


class CRUDBase(Generic[ModelType, CreateSchemaType, UpdateSchemaType]):
    """Base CRUD operations class."""

    def __init__(self, model: Type[ModelType]):
        """
        Initialize CRUD object with model class.

        Args:
            model: SQLModel class
        """
        self.model = model

    async def get(
            self,
            db: AsyncSession,
            id: int,
            load_relationships: Optional[List[str]] = None
    ) -> Optional[ModelType]:
        """
        Get a single record by ID.

        Args:
            db: Database session
            id: Record ID
            load_relationships: List of relationships to eagerly load

        Returns:
            Model instance or None
        """
        try:
            query = select(self.model).where(self.model.id == id)

            # Add relationship loading if specified
            if load_relationships:
                for rel in load_relationships:
                    if hasattr(self.model, rel):
                        query = query.options(selectinload(getattr(self.model, rel)))

            result = await db.execute(query)
            return result.scalar_one_or_none()

        except Exception as e:
            logger.error(f"Error getting {self.model.__name__} with ID {id}: {e}")
            raise

    async def get_multi(
            self,
            db: AsyncSession,
            skip: int = 0,
            limit: int = 100,
            filters: Optional[Dict[str, Any]] = None,
            order_by: Optional[str] = None,
            load_relationships: Optional[List[str]] = None
    ) -> List[ModelType]:
        """
        Get multiple records with filtering and pagination.

        Args:
            db: Database session
            skip: Number of records to skip
            limit: Maximum number of records to return
            filters: Dictionary of field filters
            order_by: Field name to order by
            load_relationships: List of relationships to eagerly load

        Returns:
            List of model instances
        """
        try:
            query = select(self.model)

            # Apply filters
            if filters:
                filter_conditions = []
                for field, value in filters.items():
                    if hasattr(self.model, field):
                        column = getattr(self.model, field)
                        if isinstance(value, list):
                            filter_conditions.append(column.in_(value))
                        elif isinstance(value, dict):
                            # Support for range queries: {"min": 10, "max": 20}
                            if "min" in value and value["min"] is not None:
                                filter_conditions.append(column >= value["min"])
                            if "max" in value and value["max"] is not None:
                                filter_conditions.append(column <= value["max"])
                        else:
                            filter_conditions.append(column == value)

                if filter_conditions:
                    query = query.where(and_(*filter_conditions))

            # Apply ordering
            if order_by:
                if order_by.startswith("-"):
                    # Descending order
                    field_name = order_by[1:]
                    if hasattr(self.model, field_name):
                        query = query.order_by(getattr(self.model, field_name).desc())
                else:
                    # Ascending order
                    if hasattr(self.model, order_by):
                        query = query.order_by(getattr(self.model, order_by))

            # Add relationship loading if specified
            if load_relationships:
                for rel in load_relationships:
                    if hasattr(self.model, rel):
                        query = query.options(selectinload(getattr(self.model, rel)))

            # Apply pagination
            query = query.offset(skip).limit(limit)

            result = await db.execute(query)
            return result.scalars().all()

        except Exception as e:
            logger.error(f"Error getting multiple {self.model.__name__} records: {e}")
            raise

    async def count(
            self,
            db: AsyncSession,
            filters: Optional[Dict[str, Any]] = None
    ) -> int:
        """
        Count records with optional filtering.

        Args:
            db: Database session
            filters: Dictionary of field filters

        Returns:
            Number of matching records
        """
        try:
            query = select(func.count()).select_from(self.model)

            # Apply filters
            if filters:
                filter_conditions = []
                for field, value in filters.items():
                    if hasattr(self.model, field):
                        column = getattr(self.model, field)
                        if isinstance(value, list):
                            filter_conditions.append(column.in_(value))
                        elif isinstance(value, dict):
                            if "min" in value and value["min"] is not None:
                                filter_conditions.append(column >= value["min"])
                            if "max" in value and value["max"] is not None:
                                filter_conditions.append(column <= value["max"])
                        else:
                            filter_conditions.append(column == value)

                if filter_conditions:
                    query = query.where(and_(*filter_conditions))

            result = await db.execute(query)
            return result.scalar()

        except Exception as e:
            logger.error(f"Error counting {self.model.__name__} records: {e}")
            raise

    async def create(
            self,
            db: AsyncSession,
            obj_in: Union[CreateSchemaType, Dict[str, Any]]
    ) -> ModelType:
        """
        Create a new record.

        Args:
            db: Database session
            obj_in: Data for new record

        Returns:
            Created model instance
        """
        try:
            if isinstance(obj_in, dict):
                obj_data = obj_in
            else:
                obj_data = obj_in.model_dump(exclude_unset=True)

            db_obj = self.model(**obj_data)
            db.add(db_obj)
            await db.commit()
            await db.refresh(db_obj)

            logger.debug(f"Created {self.model.__name__} with ID {db_obj.id}")
            return db_obj

        except Exception as e:
            await db.rollback()
            logger.error(f"Error creating {self.model.__name__}: {e}")
            raise

    async def update(
            self,
            db: AsyncSession,
            db_obj: ModelType,
            obj_in: Union[UpdateSchemaType, Dict[str, Any]]
    ) -> ModelType:
        """
        Update an existing record.

        Args:
            db: Database session
            db_obj: Existing model instance
            obj_in: Update data

        Returns:
            Updated model instance
        """
        try:
            if isinstance(obj_in, dict):
                update_data = obj_in
            else:
                update_data = obj_in.model_dump(exclude_unset=True)

            for field, value in update_data.items():
                if hasattr(db_obj, field):
                    setattr(db_obj, field, value)

            await db.commit()
            await db.refresh(db_obj)

            logger.debug(f"Updated {self.model.__name__} with ID {db_obj.id}")
            return db_obj

        except Exception as e:
            await db.rollback()
            logger.error(f"Error updating {self.model.__name__} with ID {db_obj.id}: {e}")
            raise

    async def delete(
            self,
            db: AsyncSession,
            id: int
    ) -> Optional[ModelType]:
        """
        Delete a record by ID.

        Args:
            db: Database session
            id: Record ID

        Returns:
            Deleted model instance or None
        """
        try:
            db_obj = await self.get(db, id)
            if db_obj:
                await db.delete(db_obj)
                await db.commit()
                logger.debug(f"Deleted {self.model.__name__} with ID {id}")
                return db_obj
            return None

        except Exception as e:
            await db.rollback()
            logger.error(f"Error deleting {self.model.__name__} with ID {id}: {e}")
            raise

    async def soft_delete(
            self,
            db: AsyncSession,
            id: int
    ) -> Optional[ModelType]:
        """
        Soft delete a record (if model supports it).

        Args:
            db: Database session
            id: Record ID

        Returns:
            Soft deleted model instance or None
        """
        try:
            db_obj = await self.get(db, id)
            if db_obj and hasattr(db_obj, 'is_deleted'):
                db_obj.is_deleted = True
                await db.commit()
                await db.refresh(db_obj)
                logger.debug(f"Soft deleted {self.model.__name__} with ID {id}")
                return db_obj
            return None

        except Exception as e:
            await db.rollback()
            logger.error(f"Error soft deleting {self.model.__name__} with ID {id}: {e}")
            raise

    async def restore(
            self,
            db: AsyncSession,
            id: int
    ) -> Optional[ModelType]:
        """
        Restore a soft deleted record.

        Args:
            db: Database session
            id: Record ID

        Returns:
            Restored model instance or None
        """
        try:
            db_obj = await self.get(db, id)
            if db_obj and hasattr(db_obj, 'is_deleted'):
                db_obj.is_deleted = False
                await db.commit()
                await db.refresh(db_obj)
                logger.debug(f"Restored {self.model.__name__} with ID {id}")
                return db_obj
            return None

        except Exception as e:
            await db.rollback()
            logger.error(f"Error restoring {self.model.__name__} with ID {id}: {e}")
            raise

    async def exists(
            self,
            db: AsyncSession,
            **filters
    ) -> bool:
        """
        Check if record exists with given filters.

        Args:
            db: Database session
            **filters: Field filters

        Returns:
            True if record exists, False otherwise
        """
        try:
            query = select(func.count()).select_from(self.model)

            filter_conditions = []
            for field, value in filters.items():
                if hasattr(self.model, field):
                    filter_conditions.append(getattr(self.model, field) == value)

            if filter_conditions:
                query = query.where(and_(*filter_conditions))

            result = await db.execute(query)
            count = result.scalar()
            return count > 0

        except Exception as e:
            logger.error(f"Error checking existence of {self.model.__name__}: {e}")
            raise
