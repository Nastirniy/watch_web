"""Retention: which backups to delete. Pure: no files, no clock.

Scheduled backups: the newest of each of the keep_daily newest days, keep_weekly newest ISO weeks and keep_monthly
newest months. Manual and pre-upgrade backups: kept for keep_manual_days. The newest backup is never deleted.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Hashable, Sequence

from app.backend.config import BackupConfig
from app.backend.enums.general import BackupLabel
from app.backend.services.backups.manifest import Manifest


@dataclass(frozen=True)
class RetentionRules:
    keep_daily: int
    keep_weekly: int
    keep_monthly: int
    keep_manual_days: int

    @classmethod
    def from_config(cls, cfg: BackupConfig) -> "RetentionRules":
        return cls(keep_daily=cfg.keep_daily, keep_weekly=cfg.keep_weekly, keep_monthly=cfg.keep_monthly,
                   keep_manual_days=cfg.keep_manual_days)


def _newest_per_group(newest_first: Sequence[Manifest], key: Callable[[datetime], Hashable], count: int) -> set:
    kept, groups = set(), set()
    for manifest in newest_first:
        group = key(manifest.started_at)
        if group in groups:
            continue
        if len(groups) == count:
            break
        groups.add(group)
        kept.add(manifest.id)
    return kept


def select_to_delete(manifests: Sequence[Manifest], rules: RetentionRules, now: datetime) -> list[str]:
    """Ids of the backups retention deletes, oldest first"""
    if not manifests:
        return []
    newest_first = sorted(manifests, key=lambda m: m.started_at, reverse=True)
    scheduled = [m for m in newest_first if m.label == BackupLabel.SCHEDULED]
    keep = {newest_first[0].id}
    keep |= _newest_per_group(scheduled, lambda t: t.date(), rules.keep_daily)
    keep |= _newest_per_group(scheduled, lambda t: tuple(t.isocalendar())[:2], rules.keep_weekly)
    keep |= _newest_per_group(scheduled, lambda t: (t.year, t.month), rules.keep_monthly)
    oldest_manual = now - timedelta(days=rules.keep_manual_days)
    keep |= {m.id for m in newest_first if m.label != BackupLabel.SCHEDULED and m.started_at >= oldest_manual}
    return [m.id for m in reversed(newest_first) if m.id not in keep]
