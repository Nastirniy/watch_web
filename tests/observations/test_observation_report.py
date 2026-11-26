"""Excel report: the duration column is expressed in minutes, not in the stored seconds."""
import io
from datetime import datetime, timezone
from decimal import Decimal

import openpyxl

from app.backend.enums.general import ObservationReportType
from app.backend.filters.observation_report_filter import ObservationReportCriteria
from app.backend.models.observation import Observation, ObservationFrequency
from app.backend.services.observation_report_service import (
    ObservationReportService,
    _HEADERS_ALL_FIELDS,
    _build_filter_rows,
    _fmt_minutes,
)


def test_fmt_minutes_rounds_to_one_decimal():
    assert _fmt_minutes(None) is None
    assert _fmt_minutes(0) == 0.0
    assert _fmt_minutes(45) == 0.8
    assert _fmt_minutes(60) == 1.0
    assert _fmt_minutes(150) == 2.5
    assert _fmt_minutes(3600) == 60.0


def test_filter_rows_show_duration_bounds_in_minutes():
    rows = dict(_build_filter_rows(ObservationReportCriteria(report_type=ObservationReportType.ALL_FIELDS, duration_from=90, duration_to=600)))
    assert rows["Тривалість від (хв):"] == "1.5"
    assert rows["Тривалість до (хв):"] == "10.0"
    assert not any("(сек)" in label for label in rows)


def test_all_fields_report_writes_duration_in_minutes():
    observation = Observation(
        id=1,
        user_id=1,
        post_id=1,
        signal_type_id=1,
        observed_at=datetime(2026, 9, 9, 10, 0, tzinfo=timezone.utc),
        disappeared_at=datetime(2026, 9, 9, 10, 2, 30, tzinfo=timezone.utc),
        duration_seconds=150,
    )
    band = ObservationFrequency(
        id=1,
        observation_id=1,
        kind="rc",
        frequency_min=Decimal("100.000"),
        frequency_max=Decimal("200.000"),
        frequency_center=Decimal("150.000"),
        bandwidth=Decimal("100.000"),
    )
    band.observation = observation
    params = ObservationReportCriteria(report_type=ObservationReportType.ALL_FIELDS)

    workbook = openpyxl.load_workbook(io.BytesIO(ObservationReportService.generate_excel([band], params)))
    sheet = workbook.active
    header_row = next(row for row in sheet.iter_rows(values_only=True) if row[0] == _HEADERS_ALL_FIELDS[0])
    assert "Тривалість (хв)" in header_row
    duration_col = header_row.index("Тривалість (хв)")

    data_row = next(
        row for row in sheet.iter_rows(min_row=1, values_only=True)
        if row[duration_col] not in (None, "Тривалість (хв)") and isinstance(row[duration_col], (int, float))
    )
    assert data_row[duration_col] == 2.5


def test_report_survives_dates_at_the_edge_of_the_calendar():
    """An observation stored with observed_at 9999-12-31 23:59:59 (accepted by the API) made every report that
    included it answer 500: the conversion to Kyiv time overflowed. Such a value is shown in UTC instead."""
    observation = Observation(
        id=2, user_id=1, post_id=1, signal_type_id=1,
        observed_at=datetime(9999, 12, 31, 23, 59, 59), disappeared_at=datetime(9999, 12, 31, 23, 59, 59),
        created_at=datetime(9999, 12, 31, 23, 0, 0),
    )
    band = ObservationFrequency(id=2, observation_id=2, kind="rc", frequency_min=Decimal("1.000"),
                                frequency_max=Decimal("2.000"), frequency_center=Decimal("1.500"))
    band.observation = observation
    for report_type in ObservationReportType:
        params = ObservationReportCriteria(report_type=report_type, observed_at_from=datetime(9999, 12, 31))
        workbook = openpyxl.load_workbook(io.BytesIO(ObservationReportService.generate_excel([band], params)))
        values = [v for row in workbook.active.iter_rows(values_only=True) for v in row if v is not None]
        assert "23:59:59" in values or any("31.12.9999" in str(v) for v in values), report_type
