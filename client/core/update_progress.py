"""Update download progress milestones (percentage + MiB/s).

Kept dependency-free so packaging/unit tests can import without loguru/Qt.
"""

from __future__ import annotations

# Download progress milestones: first byte (>0%), then 25/50/75, then >99%.
UPDATE_PROGRESS_MILESTONES: tuple[float, ...] = (0.0, 25.0, 50.0, 75.0, 99.0)


def format_speed_mib_s(bytes_per_sec: float) -> str:
    return f"{max(0.0, bytes_per_sec) / (1024.0 * 1024.0):.2f}MiB/s"


def next_progress_milestone(pct: float, logged: set[float]) -> float | None:
    """Return the next milestone to emit for ``pct``, or None if none are due."""
    if pct < 0:
        return None
    for mark in UPDATE_PROGRESS_MILESTONES:
        if mark in logged:
            continue
        if mark == 0.0:
            if pct > 0.0:
                return mark
            continue
        if mark == 99.0:
            if pct > 99.0:
                return mark
            continue
        if pct >= mark:
            return mark
    return None
