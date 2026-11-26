"""Backups: which backups retention keeps (spec section 2, settings)"""
from datetime import datetime, timedelta, timezone

from app.backend.config import BackupConfig
from app.backend.enums.general import BackupLabel
from app.backend.services.backups.manifest import FORMAT_VERSION, Manifest, backup_id
from app.backend.services.backups.retention import RetentionRules, select_to_delete

UTC = timezone.utc
NOW = datetime(2026, 10, 22, 12, 0, 0, tzinfo=UTC)


def m(day_offset: int, label: BackupLabel = BackupLabel.SCHEDULED, hour: int = 3) -> Manifest:
    started = (NOW - timedelta(days=day_offset)).replace(hour=hour, minute=0, second=0)
    return Manifest(
        format_version=FORMAT_VERSION, id=backup_id(started, label), label=label, started_at=started,
        finished_at=started + timedelta(minutes=1), database="db", server_product="mariadb", server_version="11.8",
        dump_tool="mariadb-dump", migration_revision="028", tables=("t",), file="dump.sql.gz", size_bytes=1,
        sha256="00" * 32,
    )


def rules(daily=0, weekly=0, monthly=0, manual_days=90) -> RetentionRules:
    return RetentionRules(keep_daily=daily, keep_weekly=weekly, keep_monthly=monthly, keep_manual_days=manual_days)


def test_rules_from_config():
    assert RetentionRules.from_config(BackupConfig()) == rules(7, 4, 6, 90)


def test_daily_keeps_newest_per_day():
    backups = [m(day, hour=hour) for day in range(10) for hour in (3, 9)]
    deleted = set(select_to_delete(backups, rules(daily=7), NOW))
    kept = {b.id for b in backups} - deleted
    assert kept == {m(day, hour=9).id for day in range(7)}


def test_weekly_and_monthly():
    backups = [m(day) for day in range(400)]
    deleted = set(select_to_delete(backups, rules(daily=7, weekly=4, monthly=6), NOW))
    newest_first = sorted(backups, key=lambda b: b.started_at, reverse=True)

    def newest_of_groups(key, count):
        seen, chosen = [], set()
        for b in newest_first:
            group = key(b.started_at)
            if group not in seen:
                seen.append(group)
                if len(seen) > count:
                    break
                chosen.add(b.id)
        return chosen

    expected = (newest_of_groups(lambda t: t.date(), 7)
                | newest_of_groups(lambda t: tuple(t.isocalendar())[:2], 4)
                | newest_of_groups(lambda t: (t.year, t.month), 6))
    assert {b.id for b in backups} - deleted == expected


def test_manual_kept_by_age():
    backups = [m(89, BackupLabel.MANUAL), m(91, BackupLabel.MANUAL), m(89, BackupLabel.PRE_UPGRADE),
               m(91, BackupLabel.PRE_UPGRADE), m(0)]
    deleted = set(select_to_delete(backups, rules(daily=1), NOW))
    assert deleted == {m(91, BackupLabel.MANUAL).id, m(91, BackupLabel.PRE_UPGRADE).id}


def test_manual_backups_do_not_count_as_daily():
    """A manual backup today does not use up today's daily slot of the scheduled one"""
    backups = [m(0, BackupLabel.MANUAL, hour=11), m(0), m(1)]
    deleted = set(select_to_delete(backups, rules(daily=1), NOW))
    assert deleted == {m(1).id}


def test_newest_never_deleted():
    old = m(400, BackupLabel.MANUAL)
    assert select_to_delete([old], rules(manual_days=0), NOW) == []
    scheduled = [m(400), m(401)]
    assert select_to_delete(scheduled, rules(), NOW) == [m(401).id]
