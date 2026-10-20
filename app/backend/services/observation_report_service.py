"""
ObservationReportService - Generates Excel (.xlsx) reports for observations.
Report language: Ukrainian. Frequency values in MHz.
Datetimes are displayed in Kyiv local time (EET UTC+2 / EEST UTC+3).

One row per frequency band (ObservationFrequency); observation columns repeat
for every band of the observation.

Three report types:
  RC_FREQ_PART    - rc bands: date of creation, start time, disappear time,
                    signal type, frequency_min, frequency_max, [spacer], comment
  VIDEO_FREQ_PART - same layout for video bands
  ALL_FIELDS      - every band: user, post, corps, brigade, signal type, band kind, emitter
                    group, frequency min/max/center, bandwidth,
                    observed/disappeared time, duration, comment,
                    creation date, tags
"""

import io
from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional, Dict
from zoneinfo import ZoneInfo

import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

from app.backend.enums.general import FrequencyKind, ObservationReportType, ScopeGroupKind, TagMatch, UnitPlacement
from app.backend.models.observation import ObservationFrequency
from app.backend.filters.observation_report_filter import ObservationReportCriteria
from app.backend.filters.simple_engine import as_criteria, stored_value
from app.backend.services.scope_groups import UNIT_KINDS
from app.backend.services.units import UnitLabel

_KYIV_TZ = ZoneInfo("Europe/Kyiv")

_HEADERS_RC_FREQ = [
    "Дата створення",
    "Час початку",
    "Час зникнення",
    "Тип сигналу",
    "Частота мін (МГц)",
    "Частота макс (МГц)",
    "",        # spacer
    "Коментар",
]

_HEADERS_VIDEO_FREQ = [
    "Дата створення",
    "Час початку",
    "Час зникнення",
    "Тип сигналу",
    "Відео частота мін (МГц)",
    "Відео частота макс (МГц)",
    "",        # spacer
    "Коментар",
]

_HEADERS_ALL_FIELDS = [
    "Користувач",
    "Пост",
    "Корпус",       # one column per unit kind (_UNIT_COLUMNS), in UNIT_KINDS order
    "Бригада",
    "Тип сигналу",
    "Тип частоти",
    "Група",
    "Частота мін (МГц)",
    "Частота макс (МГц)",
    "Частота центр (МГц)",
    "Смуга (МГц)",
    "Час спостереження",
    "Час зникнення",
    "Тривалість (хв)",
    "Коментар",
    "Дата створення",
    "Теги",
]

_COLUMN_WIDTHS_PARTIAL = [16, 12, 12, 28, 22, 22, 4, 40]
_COLUMN_WIDTHS_ALL     = [20, 20, 20, 20, 28, 12, 8, 20, 20, 20, 14, 18, 18, 16, 40, 16, 30]

_KIND_LABELS = {
    FrequencyKind.RC.value: "RC",
    FrequencyKind.VIDEO.value: "Відео",
}

# The unit columns of all_fields and the filter rows of corps_ids / brigade_ids, one per UNIT_KINDS
_UNIT_COLUMNS = {ScopeGroupKind.CORPS: "Корпус", ScopeGroupKind.BRIGADE: "Бригада"}
_UNIT_FILTER_LABELS = {ScopeGroupKind.CORPS: "Корпуси:", ScopeGroupKind.BRIGADE: "Бригади:"}
_PLACEMENT_LABELS = {
    UnitPlacement.RECORDED: "на момент створення спостереження",
    UnitPlacement.CURRENT: "поточна (де пост зараз)",
}
# Until the report is localised on the web side
ARCHIVED_UNIT_SUFFIX = " (архівовано)"


def _put(ws, row: int, column: int, value):
    """Write a value; a str is always a string cell, written unchanged.

    openpyxl would store a str starting with `=` as a formula and `#N/A` and the other error names as an error cell
    (`Cell._bind_value`); forcing the type keeps user text (comment, names, filter values) text. `quotePrefix` keeps
    it text when the cell is edited in a spreadsheet; nothing visible is added. Numbers and dates stay typed."""
    cell = ws.cell(row=row, column=column, value=value)
    if isinstance(value, str):
        cell.data_type = "s"
        cell.quotePrefix = True
    return cell


def _kind_label(kind: str) -> str:
    return _KIND_LABELS.get(kind, kind)


def _to_kyiv(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    try:
        return value.astimezone(_KYIV_TZ)
    except OverflowError:
        # A stored value at the edge of the calendar (9999-12-31 23:59) has no Kyiv time: shown in UTC
        return value


def _fmt_date(value: Optional[datetime]) -> str:
    """DD.MM.YYYY"""
    k = _to_kyiv(value)
    return k.strftime("%d.%m.%Y") if k else ""


def _fmt_time(value: Optional[datetime]) -> str:
    """HH:MM:SS"""
    k = _to_kyiv(value)
    return k.strftime("%H:%M:%S") if k else ""


def _fmt_dt(value: Optional[datetime]) -> str:
    """DD.MM.YYYY HH:MM:SS - used in header meta block"""
    k = _to_kyiv(value)
    return k.strftime("%d.%m.%Y %H:%M:%S") if k else ""


def _fmt_decimal(value: Optional[Decimal]) -> Optional[float]:
    if value is None:
        return None
    return round(float(value), 3)


def _fmt_minutes(seconds: Optional[int]) -> Optional[float]:
    """Duration stored in seconds, shown in the report as minutes with one decimal (45 s -> 0.8, 150 s -> 2.5)."""
    if seconds is None:
        return None
    return round(seconds / 60, 1)


DELETED_TAG_SUFFIX = " (видалено)"


def _tag_label(tag) -> str:
    return f"{tag.name}{DELETED_TAG_SUFFIX}" if getattr(tag, "is_deleted", False) else tag.name


def _tag_filter_label(tag_id: int, tag_names: Optional[Dict[int, str]]) -> str:
    if tag_names and tag_id in tag_names:
        return f"{tag_names[tag_id]} (#{tag_id})"
    return f"#{tag_id}"


def _unit_label(unit: Optional[UnitLabel]) -> str:
    if unit is None:
        return ""
    return f"{unit.name}{ARCHIVED_UNIT_SUFFIX}" if unit.archived else unit.name


def _unit_filter_label(unit_id: int, units: Optional[Dict[int, UnitLabel]]) -> str:
    if units and unit_id in units:
        return f"{_unit_label(units[unit_id])} (#{unit_id})"
    return f"#{unit_id}"


def _build_filter_rows(params: ObservationReportCriteria, tag_names: Optional[Dict[int, str]] = None,
                       units: Optional[Dict[int, UnitLabel]] = None) -> List[tuple]:
    """units: the labels of the corps_ids / brigade_ids (unit_labels)"""
    rows = []
    if params.observation_ids:
        rows.append(("ID спостережень:", ", ".join(str(i) for i in params.observation_ids)))
    if params.user_ids:
        rows.append(("ID користувачів:", ", ".join(str(i) for i in params.user_ids)))
    if params.post_ids:
        rows.append(("ID постів:", ", ".join(str(i) for i in params.post_ids)))
    asked = {kind: getattr(params, f"{kind.value}_ids", None) for kind in UNIT_KINDS}
    for kind, ids in asked.items():
        if ids:
            rows.append((_UNIT_FILTER_LABELS[kind], ", ".join(_unit_filter_label(i, units) for i in ids)))
    if any(asked.values()):
        rows.append(("Належність:", _PLACEMENT_LABELS[UnitPlacement(params.placement)]))
    if params.signal_type_ids:
        rows.append(("ID типів сигналів:", ", ".join(str(i) for i in params.signal_type_ids)))
    if params.statuses:
        rows.append(("Статуси:", ", ".join(stored_value(status) for status in params.statuses)))
    if params.confidence_level:
        rows.append(("Рівень впевненості:", stored_value(params.confidence_level)))
    if params.tag_ids:
        label = "Теги (усі):" if params.tag_match == TagMatch.ALL else "Теги (будь-який):"
        rows.append((label, ", ".join(_tag_filter_label(i, tag_names) for i in params.tag_ids)))
    if params.frequency_center_from is not None:
        rows.append(("Частота центр від (МГц):", str(params.frequency_center_from)))
    if params.frequency_center_to is not None:
        rows.append(("Частота центр до (МГц):", str(params.frequency_center_to)))
    if params.bandwidth_from is not None:
        rows.append(("Смуга від (МГц):", str(params.bandwidth_from)))
    if params.bandwidth_to is not None:
        rows.append(("Смуга до (МГц):", str(params.bandwidth_to)))
    if params.duration_from is not None:
        rows.append(("Тривалість від (хв):", str(_fmt_minutes(params.duration_from))))
    if params.duration_to is not None:
        rows.append(("Тривалість до (хв):", str(_fmt_minutes(params.duration_to))))
    if params.observed_at_from:
        rows.append(("Час спостереження від:", _fmt_dt(params.observed_at_from)))
    if params.observed_at_to:
        rows.append(("Час спостереження до:", _fmt_dt(params.observed_at_to)))
    if params.created_at_from:
        rows.append(("Дата створення від:", _fmt_dt(params.created_at_from)))
    if params.created_at_to:
        rows.append(("Дата створення до:", _fmt_dt(params.created_at_to)))
    if params.include_deleted:
        rows.append(("Включати видалені:", "Так"))
    if not rows:
        rows.append(("Фільтри:", "Всі записи (без обмежень)"))
    return rows


class ObservationReportService:

    _COLOR_TITLE_BG    = "1F4E79"
    _COLOR_META_BG     = "D6E4F0"
    _COLOR_COL_HDR_BG  = "2E75B6"
    _COLOR_ALT_ROW_BG  = "EBF3FB"
    _COLOR_CRITERIA_BG = "2C5F8A"
    _COLOR_SPACER_BG   = "F2F2F2"
    _COLOR_SPACER_ALT  = "E8EFF7"

    @classmethod
    def generate_excel(
        cls,
        frequencies: List[ObservationFrequency],
        params: ObservationReportCriteria,
        tag_names: Optional[Dict[int, str]] = None,
        units: Optional[Dict[int, Dict[str, UnitLabel]]] = None,
        unit_filters: Optional[Dict[int, UnitLabel]] = None,
    ) -> bytes:
        """units: observation id -> unit kind -> label for the unit columns of all_fields, as the placement says
        (services/units.py observation_unit_labels); unit_filters: the labels of corps_ids / brigade_ids"""
        params = as_criteria(ObservationReportCriteria, params)
        rtype = params.report_type
        if rtype == ObservationReportType.VIDEO_FREQ_PART:
            headers = _HEADERS_VIDEO_FREQ
            report_label = "ЗВIТ ПРО СПОСТЕРЕЖЕННЯ - ВIДЕО ЧАСТОТИ"
            col_widths = _COLUMN_WIDTHS_PARTIAL
        elif rtype == ObservationReportType.ALL_FIELDS:
            headers = _HEADERS_ALL_FIELDS
            report_label = "ЗВIТ ПРО СПОСТЕРЕЖЕННЯ - ВСI ПОЛЯ"
            col_widths = _COLUMN_WIDTHS_ALL
        else:  # RC_FREQ_PART
            headers = _HEADERS_RC_FREQ
            report_label = "ЗВIТ ПРО СПОСТЕРЕЖЕННЯ - RC ЧАСТОТИ"
            col_widths = _COLUMN_WIDTHS_PARTIAL

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Звіт спостережень"

        def _font(bold=False, size=10, color="000000"):
            return Font(name="Arial", bold=bold, size=size, color=color)

        def _fill(hex_color):
            return PatternFill(fill_type="solid", fgColor=hex_color)

        thin = Side(style="thin")
        thin_border = Border(left=thin, right=thin, top=thin, bottom=thin)
        center = Alignment(horizontal="center", vertical="center", wrap_text=True)
        left   = Alignment(horizontal="left",   vertical="center", wrap_text=True)

        num_cols = len(headers)
        last_col = get_column_letter(num_cols)

        def _merge(row: int):
            ws.merge_cells(f"A{row}:{last_col}{row}")

        row = 1

        # --- Title ---
        _merge(row)
        c = ws.cell(row=row, column=1, value=report_label)
        c.font = _font(bold=True, size=13, color="FFFFFF")
        c.fill = _fill(cls._COLOR_TITLE_BG)
        c.alignment = center
        ws.row_dimensions[row].height = 26
        row += 1

        # --- Generation time ---
        _merge(row)
        c = ws.cell(row=row, column=1,
                    value=f"Дата формування: {_fmt_dt(datetime.now(timezone.utc))} (за київським часом)")
        c.font = _font(bold=True, size=10)
        c.fill = _fill(cls._COLOR_META_BG)
        c.alignment = left
        row += 1

        # --- Record count ---
        _merge(row)
        observation_count = len({f.observation_id for f in frequencies})
        c = ws.cell(row=row, column=1,
                    value=f"Загальна кiлькiсть записiв: {len(frequencies)} (спостережень: {observation_count})")
        c.font = _font(bold=True, size=10)
        c.fill = _fill(cls._COLOR_META_BG)
        c.alignment = left
        row += 1

        row += 1  # blank

        # --- Filter criteria heading ---
        _merge(row)
        c = ws.cell(row=row, column=1, value="Критерiї вiдбору даних:")
        c.font = _font(bold=True, size=11, color="FFFFFF")
        c.fill = _fill(cls._COLOR_CRITERIA_BG)
        c.alignment = left
        ws.row_dimensions[row].height = 20
        row += 1

        # --- Filter rows ---
        for label, value in _build_filter_rows(params, tag_names, unit_filters):
            c_lbl = ws.cell(row=row, column=1, value=label)
            c_lbl.font = _font(bold=True, size=10)
            c_lbl.fill = _fill(cls._COLOR_META_BG)
            c_lbl.alignment = left
            if num_cols > 1:
                ws.merge_cells(f"B{row}:{last_col}{row}")
            c_val = _put(ws, row, 2, value)
            c_val.font = _font(size=10)
            c_val.alignment = left
            row += 1

        row += 1  # blank

        # --- Column headers ---
        header_row = row
        for col_idx, header in enumerate(headers, start=1):
            c = ws.cell(row=row, column=col_idx, value=header)
            c.border = thin_border
            if header == "":
                c.fill = _fill(cls._COLOR_SPACER_BG)
            else:
                c.font = _font(bold=True, size=10, color="FFFFFF")
                c.fill = _fill(cls._COLOR_COL_HDR_BG)
                c.alignment = center
        ws.row_dimensions[row].height = 36
        row += 1

        # --- Data rows ---
        spacer_col = 7 if rtype != ObservationReportType.ALL_FIELDS else None

        for i, band in enumerate(frequencies):
            use_alt = i % 2 == 1
            obs = band.observation

            signal_name = obs.signal_type.name if obs.signal_type else str(obs.signal_type_id)

            if rtype == ObservationReportType.ALL_FIELDS:
                user_name = (
                    obs.user.username_alias
                    if obs.user and obs.user.username_alias
                    else (obs.user.username if obs.user else str(obs.user_id))
                )
                post_name = obs.post.name if obs.post else str(obs.post_id)
                tags_str = ", ".join(_tag_label(t) for t in obs.tags) if obs.tags else ""
                observation_units = (units or {}).get(obs.id, {})
                row_data = [
                    user_name,
                    post_name,
                    *(_unit_label(observation_units.get(kind.value)) for kind in UNIT_KINDS),
                    signal_name,
                    _kind_label(band.kind),
                    band.link_group,
                    _fmt_decimal(band.frequency_min),
                    _fmt_decimal(band.frequency_max),
                    _fmt_decimal(band.frequency_center),
                    _fmt_decimal(band.bandwidth),
                    _fmt_time(obs.observed_at),
                    _fmt_time(obs.disappeared_at),
                    _fmt_minutes(obs.duration_seconds),
                    obs.comment or "",
                    _fmt_date(obs.created_at),
                    tags_str,
                ]
            else:  # RC_FREQ_PART / VIDEO_FREQ_PART: the query already selected the kind
                row_data = [
                    _fmt_date(obs.created_at),
                    _fmt_time(obs.observed_at),
                    _fmt_time(obs.disappeared_at),
                    signal_name,
                    _fmt_decimal(band.frequency_min),
                    _fmt_decimal(band.frequency_max),
                    "",
                    obs.comment or "",
                ]

            for col_idx, value in enumerate(row_data, start=1):
                c = _put(ws, row, col_idx, value)
                c.border = thin_border
                if spacer_col and col_idx == spacer_col:
                    c.fill = _fill(cls._COLOR_SPACER_ALT if use_alt else cls._COLOR_SPACER_BG)
                else:
                    c.font = _font(size=10)
                    c.alignment = left
                    if use_alt:
                        c.fill = _fill(cls._COLOR_ALT_ROW_BG)

            row += 1

        # --- Column widths ---
        for col_idx, width in enumerate(col_widths, start=1):
            ws.column_dimensions[get_column_letter(col_idx)].width = width

        # --- Freeze above data ---
        ws.freeze_panes = ws.cell(row=header_row + 1, column=1)

        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        return buf.getvalue()