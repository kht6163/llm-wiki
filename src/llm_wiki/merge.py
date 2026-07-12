"""Deterministic, line-preserving three-way text merge."""

from __future__ import annotations

from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from typing import Literal, NamedTuple


@dataclass(frozen=True, slots=True)
class MergeHunk:
    """One ambiguous region that requires an explicit resolution."""

    start_line: int
    base: str
    mine: str
    current: str
    resolved: str | None
    merged_start: int | None = None  # Python code-point offset in MergeResult.text


@dataclass(frozen=True, slots=True)
class MergeResult:
    """Merged text plus unresolved conflicts in source order."""

    text: str
    conflicts: tuple[MergeHunk, ...]


class _Edit(NamedTuple):
    side: Literal["mine", "current"]
    start: int
    end: int
    replacement: tuple[str, ...]


class _Region(NamedTuple):
    start: int
    end: int
    replacement: tuple[str, ...]
    conflict: MergeHunk | None


def _changed_regions(
    base: tuple[str, ...], changed: tuple[str, ...], side: Literal["mine", "current"]
) -> list[_Edit]:
    matcher = SequenceMatcher(None, base, changed, autojunk=False)
    return [
        _Edit(side, base_start, base_end, changed[changed_start:changed_end])
        for tag, base_start, base_end, changed_start, changed_end in matcher.get_opcodes()
        if tag != "equal"
    ]


def _interacts(left: _Edit, right: _Edit) -> bool:
    left_insert = left.start == left.end
    right_insert = right.start == right.end
    if left_insert and right_insert:
        return left.start == right.start
    if left_insert:
        return right.start < left.start < right.end
    if right_insert:
        return left.start < right.start < left.end
    return max(left.start, right.start) < min(left.end, right.end)


def _components(edits: list[_Edit]) -> list[list[_Edit]]:
    pending = sorted(edits, key=lambda edit: (edit.start, edit.end, edit.side))
    components: list[list[_Edit]] = []
    while pending:
        component = [pending.pop(0)]
        while True:
            connected = [
                candidate
                for candidate in pending
                if any(_interacts(candidate, member) for member in component)
            ]
            if not connected:
                break
            component.extend(connected)
            pending = [candidate for candidate in pending if candidate not in connected]
        components.append(component)
    return components


def _apply_edits(
    base: tuple[str, ...], start: int, end: int, edits: list[_Edit]
) -> tuple[str, ...]:
    output: list[str] = []
    cursor = start
    for edit in sorted(edits, key=lambda item: (item.start, item.end)):
        output.extend(base[cursor : edit.start])
        output.extend(edit.replacement)
        cursor = edit.end
    output.extend(base[cursor:end])
    return tuple(output)


def _merge_component(base: tuple[str, ...], component: list[_Edit]) -> _Region:
    start = min(edit.start for edit in component)
    end = max(edit.end for edit in component)
    mine_edits = [edit for edit in component if edit.side == "mine"]
    current_edits = [edit for edit in component if edit.side == "current"]
    mine = _apply_edits(base, start, end, mine_edits)
    current = _apply_edits(base, start, end, current_edits)

    if not mine_edits:
        return _Region(start, end, current, None)
    if not current_edits or mine == current:
        return _Region(start, end, mine, None)

    conflict = MergeHunk(
        start_line=start + 1,
        base="".join(base[start:end]),
        mine="".join(mine),
        current="".join(current),
        resolved=None,
    )
    return _Region(start, end, base[start:end], conflict)


def three_way_merge(base: str, mine: str, current: str) -> MergeResult:
    """Merge independent edits and report every ambiguous overlapping region."""

    if mine == current:
        return MergeResult(mine, ())
    if mine == base:
        return MergeResult(current, ())
    if current == base:
        return MergeResult(mine, ())

    base_lines = tuple(base.splitlines(keepends=True))
    mine_lines = tuple(mine.splitlines(keepends=True))
    current_lines = tuple(current.splitlines(keepends=True))
    edits = _changed_regions(base_lines, mine_lines, "mine")
    edits.extend(_changed_regions(base_lines, current_lines, "current"))
    regions = sorted(
        (_merge_component(base_lines, component) for component in _components(edits)),
        key=lambda region: (region.start, region.end),
    )

    output: list[str] = []
    conflicts: list[MergeHunk] = []
    cursor = 0
    output_length = 0
    for region in regions:
        unchanged = base_lines[cursor : region.start]
        output.extend(unchanged)
        output_length += sum(len(line) for line in unchanged)
        merged_start = output_length
        output.extend(region.replacement)
        output_length += sum(len(line) for line in region.replacement)
        cursor = region.end
        if region.conflict is not None:
            conflicts.append(replace(region.conflict, merged_start=merged_start))
    output.extend(base_lines[cursor:])
    return MergeResult("".join(output), tuple(conflicts))
