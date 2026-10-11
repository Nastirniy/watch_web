"""Filter engine: a total ORDER BY (primary key last), NULLS FIRST/LAST that MySQL accepts, LIKE escaping and
the paging bounds of lists, including GET /categories/{id}/signal-types."""
import pytest
from sqlalchemy.dialects import mysql
from sqlmodel import select

from app.backend.api.v1 import V1
from app.backend.api.v1.models.query.base import BaseFilterParams
from app.backend.enums.general import SortOrder
from app.backend.enums.routes import CategoryRoutes, TagRoutes
from app.backend.filters.category_filter import CategoryFilterService, CategoryCriteria
from app.backend.filters.signal_type_filter import SignalTypeFilterService, SignalTypeCriteria
from app.backend.filters.tag_filter import TagFilterService, TagCriteria
from app.backend.models.observation import Category, SignalType, Tag
from tests.helpers import make_user


def _mysql_sql(query) -> str:
    return str(query.compile(dialect=mysql.dialect(), compile_kwargs={"literal_binds": True}))


def _order_by(sql: str) -> str:
    return sql.split("ORDER BY", 1)[1].split("LIMIT", 1)[0]


def test_no_sort_by_orders_by_primary_key():
    sql = _mysql_sql(TagFilterService.apply_filters(select(Tag), TagCriteria()))
    assert _order_by(sql).strip() == "tags.id"


def test_sort_by_ends_with_primary_key():
    sql = _mysql_sql(TagFilterService.apply_filters(select(Tag), TagCriteria(sort_by="name", sort_order=SortOrder.DESCENDING)))
    assert _order_by(sql).replace("\n", "").split() == ["tags.name", "DESC,", "tags.id"]


def test_count_query_has_no_order_by():
    sql = _mysql_sql(TagFilterService.apply_filters_for_count(select(Tag), TagCriteria()))
    assert "ORDER BY" not in sql


@pytest.mark.parametrize("order", list(SortOrder))
def test_sort_orders_compile_for_mysql_without_nulls_keyword(order):
    criteria = SignalTypeCriteria(sort_by="category_id", sort_order=order)
    sql = _mysql_sql(SignalTypeFilterService.apply_filters(select(SignalType), criteria))
    assert "NULLS" not in sql
    assert _order_by(sql).strip().endswith("signal_types.id")


@pytest.mark.asyncio
async def test_sort_orders_on_sqlite(db):
    first = await _signal(db, "A", None)
    second = await _signal(db, "B", 1)
    third = await _signal(db, "C", 2)
    fourth = await _signal(db, "D", None)
    expected = {
        SortOrder.ASCENDING: [first, fourth, second, third],
        SortOrder.DESCENDING: [third, second, first, fourth],
        SortOrder.ASCENDING_NULLS_FIRST: [first, fourth, second, third],
        SortOrder.ASCENDING_NULLS_LAST: [second, third, first, fourth],
        SortOrder.DESCENDING_NULLS_FIRST: [first, fourth, third, second],
        SortOrder.DESCENDING_NULLS_LAST: [third, second, first, fourth],
    }
    for order, ids in expected.items():
        criteria = SignalTypeCriteria(sort_by="category_id", sort_order=order, page_size=10)
        rows = (await db.exec(SignalTypeFilterService.apply_filters(select(SignalType), criteria))).all()
        assert [row.id for row in rows] == ids, order


async def _signal(db, code, category_id) -> int:
    row = SignalType(code=code, name=code, category_id=category_id)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row.id


@pytest.mark.asyncio
async def test_ties_page_without_repeats_or_gaps(db):
    ids = [await _signal(db, f"T{i}", None) for i in range(5)]
    seen = []
    for page in (1, 2, 3):
        criteria = SignalTypeCriteria(sort_by="category_id", page=page, page_size=2)
        rows = (await db.exec(SignalTypeFilterService.apply_filters(select(SignalType), criteria))).all()
        seen += [row.id for row in rows]
    assert seen == ids


@pytest.mark.asyncio
async def test_contains_escapes_like_wildcards(db):
    for name in ("a_b", "axb", "100%", "1000", "back\\slash", "backXslash"):
        db.add(Tag(name=name))
    await db.commit()

    async def names(text):
        rows = (await db.exec(TagFilterService.apply_filters(select(Tag), TagCriteria(name_contains=text)))).all()
        return sorted(row.name for row in rows)

    assert await names("a_b") == ["a_b"]
    assert await names("100%") == ["100%"]
    assert await names("%") == ["100%"]
    assert await names("\\") == ["back\\slash"]


def test_contains_passes_escape_to_mysql():
    sql = _mysql_sql(TagFilterService.apply_filters(select(Tag), TagCriteria(name_contains="a_b")))
    assert "a\\\\_b" in sql
    assert "ESCAPE" in sql


def test_page_has_an_upper_bound():
    BaseFilterParams(page=1_000_000)
    with pytest.raises(ValueError):
        BaseFilterParams(page=1_000_001)


@pytest.mark.asyncio
async def test_huge_page_is_422(client, db, headers):
    user = await make_user(db, perms=["tag.view"])
    response = await client.get(V1.path(TagRoutes.LIST_ALL), params={"page": 10**15}, headers=headers(user))
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_category_signal_types_is_paged_and_ordered(client, db, headers):
    category = Category(name="C", alias="c")
    db.add(category)
    await db.commit()
    await db.refresh(category)
    for code, order in (("S1", 2), ("S2", 1), ("S3", 1), ("S4", 0)):
        db.add(SignalType(code=code, name=code, category_id=category.id, sort_order=order))
    await db.commit()
    user = await make_user(db, perms=["category.view", "signal_type.view"])
    url = V1.path(CategoryRoutes.CATEGORY_SIGNAL_TYPES, category_id=category.id)

    response = await client.get(url, headers=headers(user))
    assert [row["code"] for row in response.json()] == ["S4", "S2", "S3", "S1"]

    response = await client.get(url, params={"page": 2, "page_size": 2}, headers=headers(user))
    assert [row["code"] for row in response.json()] == ["S3", "S1"]

    for params in ({"page_size": 0}, {"page_size": 201}, {"page": 0}, {"page": 10**15}):
        response = await client.get(url, params=params, headers=headers(user))
        assert response.status_code == 422, params
