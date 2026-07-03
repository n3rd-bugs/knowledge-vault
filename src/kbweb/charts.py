"""kbweb.charts — pure chart-row builders for the dashboard (no I/O, Inv. 11).

Everything renders as HTML/CSS bars (dataviz: magnitude → horizontal bar rows
with one sequential hue; part-to-whole → one stacked bar with 2px gaps and a
text legend). These builders only compute structure; templates draw it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BarRow:
    label: str
    count: int
    pct: float  # 0–100: bar rows scale vs the max row; stacked segments vs the total
    href: str | None = None


def bar_rows(
    counts: dict[str, int], href_fmt: str | None = None, max_rows: int = 12
) -> list[BarRow]:
    """Sorted magnitude rows; the tail beyond ``max_rows`` folds into one row.

    The folded row is labeled "(N more)" and carries no link — silent truncation
    would misread as full coverage (dataviz: no silent caps).
    """
    rows = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    shown = rows[:max_rows]
    rest = rows[max_rows:]
    if rest:
        shown.append((f"({len(rest)} more)", sum(n for _, n in rest)))
    top = max((n for _, n in shown), default=0) or 1
    out: list[BarRow] = []
    for label, n in shown:
        href = href_fmt.format(label) if href_fmt and not label.startswith("(") else None
        out.append(BarRow(label=label, count=n, pct=round(100 * n / top, 1), href=href))
    return out


def stacked_segments(counts: dict[str, int], order: tuple[str, ...]) -> list[BarRow]:
    """Part-to-whole segments in a fixed semantic order (color follows the
    entity, never its rank); zero-count states are skipped; unknown states
    append at the end so nothing is silently dropped."""
    total = sum(counts.values()) or 1
    keys = [k for k in order if counts.get(k)]
    keys += [k for k in counts if k not in order and counts[k]]
    return [BarRow(label=k, count=counts[k], pct=round(100 * counts[k] / total, 1)) for k in keys]
