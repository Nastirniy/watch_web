"""Excel report: at most settings.reports.max_rows band rows, styles built once per report, unit labels read only for
the report that shows them, a filter unit named only where both report permissions cover it."""
import io
from datetime import datetime, timezone
from decimal import Decimal

import openpyxl

from app.backend.api.v1 import V1
from app.backend.api.v1 import observations as observations_routes
from app.backend.auth.scope import Node
from app.backend.config import settings
from app.backend.enums.general import ObservationReportType, Permissions
from app.backend.enums.routes import ObservationRoutes
from app.backend.filters.observation_report_filter import ObservationReportCriteria
from app.backend.models.observation import Observation, ObservationFrequency
from app.backend.services import observation_report_service as report_service
from tests.helpers import (assign, make_admin, make_dictionary, make_group, make_post, make_role, make_user,
                           observation_payload)

REPORT = V1.path(ObservationRoutes.REPORT)


async def _observe(client, h, post, signal_type_id, count):
    for i in range(count):
        r = await client.post(V1.path(ObservationRoutes.CREATE),
                              json={**observation_payload(post.id, signal_type_id), "comment": f"o{i}"}, headers=h)
        assert r.status_code == 200, r.text


def _rows(response, first_header):
    assert response.status_code == 200, response.text
    rows = list(openpyxl.load_workbook(io.BytesIO(response.content)).active.iter_rows(values_only=True))
    header_at = next(i for i, row in enumerate(rows) if row[0] == first_header)
    return rows[:header_at], rows[header_at + 1:]


async def test_report_at_the_limit_succeeds(client, db, headers, monkeypatch):
    monkeypatch.setattr(settings.reports, "max_rows", 3)
    h = headers(await make_admin(db))
    ids = await make_dictionary(db)
    await _observe(client, h, await make_post(db), ids["signal_type_id"], 3)
    response = await client.post(REPORT, json={"report_type": "all_fields"}, headers=h)
    _, data = _rows(response, "Користувач")
    assert len(data) == 3


async def test_report_over_the_limit_answers_422(client, db, headers, monkeypatch):
    monkeypatch.setattr(settings.reports, "max_rows", 3)
    h = headers(await make_admin(db))
    ids = await make_dictionary(db)
    await _observe(client, h, await make_post(db), ids["signal_type_id"], 4)
    response = await client.post(REPORT, json={"report_type": "all_fields"}, headers=h)
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "report_too_large"
    assert body["max_rows"] == 3


async def test_rc_report_reads_no_unit_labels(client, db, headers, monkeypatch):
    h = headers(await make_admin(db))
    ids = await make_dictionary(db)
    corps = await make_group(db, name="Corps 1", kind="corps")
    await _observe(client, h, await make_post(db, group_id=corps.id), ids["signal_type_id"], 1)

    async def refuse(*args, **kwargs):
        raise AssertionError("observation_unit_labels is not needed by the rc report")

    monkeypatch.setattr(observations_routes, "observation_unit_labels", refuse)
    response = await client.post(REPORT, json={"report_type": "rc_freq_part", "corps_ids": [corps.id]}, headers=h)
    _, data = _rows(response, "Дата створення")
    assert len(data) == 1


async def test_report_names_a_filter_unit_only_where_export_create_is_held_too(client, db, headers):
    corps_a = await make_group(db, name="Corps A", kind="corps")
    elsewhere = await make_post(db, name="Elsewhere")
    view_only_at_a = await make_role(db, [Permissions.OBSERVATION_VIEW])
    export_elsewhere = await make_role(db, [Permissions.EXPORT_CREATE])
    both = await make_role(db, [Permissions.OBSERVATION_VIEW, Permissions.EXPORT_CREATE])

    split = await make_user(db)
    await assign(db, split, view_only_at_a, Node.group(corps_a.id))
    await assign(db, split, export_elsewhere, Node.post(elsewhere.id))
    whole = await make_user(db)
    await assign(db, whole, both, Node.group(corps_a.id))

    async def unit_row(user):
        response = await client.post(REPORT, json={"report_type": "all_fields", "corps_ids": [corps_a.id]},
                                     headers=headers(user))
        top, _ = _rows(response, "Користувач")
        return next(row[1] for row in top if row[0] == "Корпуси:")

    assert await unit_row(split) == f"#{corps_a.id}"
    assert await unit_row(whole) == f"Corps A (#{corps_a.id})"


def _band(i):
    observation = Observation(
        id=i, user_id=1, post_id=1, signal_type_id=1, comment=f"c{i}",
        observed_at=datetime(2026, 9, 9, 10, 0, tzinfo=timezone.utc),
        disappeared_at=datetime(2026, 9, 9, 10, 2, 30, tzinfo=timezone.utc),
        created_at=datetime(2026, 9, 9, 10, 0, tzinfo=timezone.utc), duration_seconds=150)
    band = ObservationFrequency(id=i, observation_id=i, kind="rc", frequency_min=Decimal("100.000"),
                                frequency_max=Decimal("200.000"), frequency_center=Decimal("150.000"),
                                bandwidth=Decimal("100.000"))
    band.observation = observation
    return band


def test_styles_are_built_once_per_report(monkeypatch):
    built = {"Font": 0, "PatternFill": 0, "Alignment": 0, "Border": 0}

    def counting(name):
        original = getattr(report_service, name)

        def build(*args, **kwargs):
            built[name] += 1
            return original(*args, **kwargs)

        return build

    for name in built:
        monkeypatch.setattr(report_service, name, counting(name))

    def count_for(bands):
        for name in built:
            built[name] = 0
        report_service.ObservationReportService.generate_excel(
            [_band(i) for i in range(bands)],
            ObservationReportCriteria(report_type=ObservationReportType.ALL_FIELDS))
        return dict(built)

    assert count_for(50) == count_for(5)


def test_rows_keep_their_look_when_cells_share_styles():
    """Data cells take the style of the first cell that looks the same: fill by row parity, a text cell stays text"""
    bands = [_band(i) for i in range(1, 5)]
    bands[1].observation.comment = "=SUM(A1)"
    out = report_service.ObservationReportService.generate_excel(
        bands, ObservationReportCriteria(report_type=ObservationReportType.ALL_FIELDS))
    sheet = openpyxl.load_workbook(io.BytesIO(out)).active
    first = next(row[0].row for row in sheet.iter_rows() if row[0].value == "Користувач") + 1
    plain, alt = sheet.cell(first, 1), sheet.cell(first + 1, 1)
    assert plain.fill.fill_type is None and alt.fill.fgColor.rgb.endswith("EBF3FB")
    assert alt.font.name == "Arial" and alt.border.left.style == "thin" and alt.alignment.wrap_text
    comment = sheet.cell(first + 1, 15)
    assert comment.value == "=SUM(A1)" and comment.data_type == "s" and comment.quotePrefix
    assert not sheet.cell(first, 8).quotePrefix            # a number
    assert sheet.cell(first + 2, 1).fill.fill_type is None  # parity goes on
