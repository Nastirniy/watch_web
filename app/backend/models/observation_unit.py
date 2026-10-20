from datetime import datetime
from sqlalchemy import Index
from sqlmodel import Field, SQLModel


class ObservationUnit(SQLModel, table=True):
    """A unit (corps, brigade: a scope group of a unit kind) the observation's post was in when the observation was
    created: one row per unit above the post, written in the create transaction, or by `cli/units.py fill-history`
    for an observation that had no row at all (every unit its post was in at the run). Rows are never changed or
    removed afterwards (except the physical removal named below); an observation that has any row is never given
    another one.

    Owned by the observation: archiving anything (observation, post, unit) never touches these rows; a physical
    removal of observations removes them in the same transaction (services/units.py delete_observation_units).
    No foreign keys: groups are archived, never deleted, so the ids stay valid, and an insert takes no lock on the
    unit or the observation row. created_at is the observation's: per-unit counters read a range of the primary key.
    """
    __tablename__ = "observation_units"
    __table_args__ = (Index("ix_observation_units_observation", "observation_id", "group_id"),)

    group_id: int = Field(primary_key=True, sa_column_kwargs={"autoincrement": False})
    created_at: datetime = Field(primary_key=True)
    observation_id: int = Field(primary_key=True, sa_column_kwargs={"autoincrement": False})
