"""Late commits. An auto-increment id is taken at insert and visible at commit, so id 8 can appear after id 9, and a
reader that keeps only id > 9 never sees 8 (proved: 23% lost under a harsh load). Every id skipped is watched here
until it arrives or ttl passes (then it was a rollback). The resume position stays below a gap only for hold, so a
rollback does not pin every reconnecting client to a minute of duplicates; after that the cursor names the gap as a
late id (cursor(), ring.py), so a reconnect after a late commit still gets the row. A jump above max_jump is no gap: a restore
raised AUTO_INCREMENT. Never a fixed number of slots in the query: dead gaps would fill them and stop the search."""
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Tuple


@dataclass
class GapTracker:
    ttl: float
    hold: float
    max_jump: int
    max_gaps: int
    head: int = 0
    _open: Dict[int, float] = field(default_factory=dict)       # id -> when it was first missed

    def saw(self, ids: Iterable[int], now: float) -> None:
        for row_id in sorted(ids):
            if row_id > self.head:
                if row_id - self.head - 1 <= self.max_jump:
                    self._open.update((missing, now) for missing in range(self.head + 1, row_id))
                self.head = row_id
            else:
                self._open.pop(row_id, None)

    def expire(self, now: float) -> int:
        """Gaps older than ttl are rollbacks; above max_gaps the oldest ids go. The number of gaps given up"""
        dead = [row_id for row_id, seen in self._open.items() if now - seen >= self.ttl]
        for row_id in dead:
            del self._open[row_id]
        while len(self._open) > self.max_gaps:
            del self._open[min(self._open)]
        return len(dead)

    def open_ids(self) -> List[int]:
        return sorted(self._open)

    def position(self, now: float) -> int:
        """Every committed row up to here was read: the head, or just below the lowest gap younger than hold"""
        fresh = [row_id for row_id, seen in self._open.items() if now - seen < self.hold]
        return min(fresh) - 1 if fresh else self.head

    def cursor(self, now: float, max_late: int) -> Tuple[int, List[int]]:
        """The position and the late ids: gaps the position passed after hold, still watched (ring.py). With more
        than max_late of them the position stays below the lowest: duplicates, never a lost row"""
        position = self.position(now)
        late = sorted(row_id for row_id in self._open if row_id <= position)
        if len(late) > max_late:
            return late[0] - 1, []
        return position, late
