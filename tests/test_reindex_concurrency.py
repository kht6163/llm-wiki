"""Deterministic concurrency contracts for external Markdown reconciliation."""

from __future__ import annotations

import os
import stat
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from llm_wiki import file_projection as fp
from llm_wiki.util import path_norm

_EVENT_TIMEOUT = 5.0


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _replace(path: Path, body: str, *, generation: int) -> None:
    replacement = path.with_name(f".{path.name}.{generation}.swap")
    replacement.write_text(body, encoding="utf-8")
    os.replace(replacement, path)


def _run_on_first_regular_fstat(
    monkeypatch: pytest.MonkeyPatch,
    action: Callable[[], None],
) -> tuple[threading.Thread, list[BaseException]]:
    """Pause the stable reader after its first file fstat until ``action`` finishes."""
    observed = threading.Event()
    finished = threading.Event()
    errors: list[BaseException] = []
    real_fstat = fp.os.fstat
    armed = True

    def mutate() -> None:
        if not observed.wait(_EVENT_TIMEOUT):
            errors.append(AssertionError("stable reader never fstat-ed the Markdown file"))
            return
        try:
            action()
        except BaseException as exc:  # pragma: no cover - re-raised in the test thread
            errors.append(exc)
        finally:
            finished.set()

    def gated_fstat(fd: int) -> os.stat_result:
        nonlocal armed
        value = real_fstat(fd)
        if armed and stat.S_ISREG(value.st_mode):
            armed = False
            observed.set()
            if not finished.wait(_EVENT_TIMEOUT):
                raise AssertionError("file mutation did not finish while stable read was paused")
        return value

    monkeypatch.setattr(fp.os, "fstat", gated_fstat)
    thread = threading.Thread(target=mutate, daemon=True)
    thread.start()
    return thread, errors


def _run_after_first_stable_read(
    monkeypatch: pytest.MonkeyPatch,
    action: Callable[[], None],
) -> tuple[threading.Thread, list[BaseException], list[Path]]:
    """Run a concurrent managed write after the first complete stable file snapshot."""
    observed = threading.Event()
    finished = threading.Event()
    errors: list[BaseException] = []
    reads: list[Path] = []
    real_read = fp.read_stable_markdown

    def race() -> None:
        if not observed.wait(_EVENT_TIMEOUT):
            errors.append(AssertionError("reindex never completed a stable Markdown read"))
            return
        try:
            action()
        except BaseException as exc:  # pragma: no cover - re-raised in the test thread
            errors.append(exc)
        finally:
            finished.set()

    def gated_read(vault: Path, path: Path):
        result = real_read(vault, path)
        reads.append(Path(path))
        if len(reads) == 1:
            observed.set()
            if not finished.wait(_EVENT_TIMEOUT):
                raise AssertionError("managed race did not finish after stable read")
        return result

    monkeypatch.setattr(fp, "read_stable_markdown", gated_read)
    thread = threading.Thread(target=race, daemon=True)
    thread.start()
    return thread, errors, reads


def _join_race(thread: threading.Thread, errors: list[BaseException]) -> None:
    thread.join(_EVENT_TIMEOUT)
    assert not thread.is_alive(), "concurrent test action did not terminate"
    if errors:
        raise errors[0]


def test_stable_read_rejects_atomic_replace_during_read(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    target = vault / "atomic.md"
    _write(target, "# Before\n\nfirst generation")

    thread, errors = _run_on_first_regular_fstat(
        monkeypatch,
        lambda: _replace(target, "# After\n\nreplacement generation", generation=1),
    )

    with pytest.raises(fp.StableFileError) as raised:
        fp.read_stable_markdown(vault, target)

    _join_race(thread, errors)
    assert raised.value.reason == "file_changed"


def test_stable_read_rejects_same_length_in_place_mutation(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    target = vault / "in-place.md"
    before = b"# Note\n\nalpha-0000\n"
    after = b"# Note\n\nbravo-0000\n"
    assert len(before) == len(after)
    target.write_bytes(before)
    initial = target.stat()

    def mutate_in_place() -> None:
        with target.open("r+b", buffering=0) as stream:
            stream.write(after)
            os.fsync(stream.fileno())
        os.utime(
            target,
            ns=(initial.st_atime_ns, initial.st_mtime_ns + 1_000_000_000),
        )

    thread, errors = _run_on_first_regular_fstat(monkeypatch, mutate_in_place)

    with pytest.raises(fp.StableFileError) as raised:
        fp.read_stable_markdown(vault, target)

    _join_race(thread, errors)
    assert raised.value.reason == "file_changed"
    assert target.read_bytes() == after


def test_stable_read_rejects_symlinked_parent(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    _write(outside / "secret.md", "outside vault")
    (vault / "linkdir").symlink_to(outside, target_is_directory=True)

    with pytest.raises(fp.StableFileError) as raised:
        fp.read_stable_markdown(
            vault,
            vault / "linkdir" / "secret.md",
        )

    assert raised.value.reason == "file_unreadable"


def test_existing_reconcile_retries_exact_tuple_after_managed_update(
    ctx, principals, monkeypatch
):
    docs = ctx.docs
    editor = principals["editor"]
    target = ctx.settings.vault_path / "existing-race.md"
    docs.create(editor, "existing-race.md", "# Managed v1\n\nbase", embed=False)
    _write(target, "# External\n\nstale snapshot")

    thread, errors, reads = _run_after_first_stable_read(
        monkeypatch,
        lambda: docs.update(
            editor,
            "existing-race.md",
            1,
            "# Managed v2\n\ncanonical",
            embed=False,
        ),
    )

    report = docs.reindex_all()

    _join_race(thread, errors)
    assert len(reads) >= 2
    assert report["retried"] == 1
    assert report["updated"] == 0
    assert report["unchanged"] == 1
    assert report["skipped_conflicts"] == []
    current = docs.get("existing-race.md")
    assert current["version"] == 2
    assert current["content"] == "# Managed v2\n\ncanonical"
    assert target.read_text(encoding="utf-8") == current["content"]
    with ctx.db.reader() as conn:
        revisions = conn.execute(
            "SELECT version, op, body FROM revisions WHERE doc_id="
            "(SELECT id FROM documents WHERE path_norm=?) ORDER BY version",
            (path_norm("existing-race.md"),),
        ).fetchall()
    assert [(row["version"], row["op"]) for row in revisions] == [
        (1, "create"),
        (2, "edit"),
    ]


def test_pending_target_projects_canonical_body_before_disk_import(
    ctx, principals, monkeypatch
):
    docs = ctx.docs
    target = ctx.settings.vault_path / "pending.md"
    canonical = "# Canonical\n\nmanaged body"
    stale = "# External\n\nstale disk body"
    docs.create(principals["editor"], "pending.md", canonical, embed=False)
    with ctx.db.writer() as conn:
        conn.execute(
            "UPDATE documents SET file_state='pending' WHERE path_norm=?",
            (path_norm("pending.md"),),
        )
    _write(target, stale)

    order: list[str] = []
    real_project = docs._project_current
    real_read = fp.read_stable_markdown

    def tracked_project(doc_id: int, *, max_attempts: int = 3):
        order.append("project")
        assert target.read_text(encoding="utf-8") == stale
        return real_project(doc_id, max_attempts=max_attempts)

    def tracked_read(vault: Path, path: Path):
        order.append("read")
        assert "project" in order
        assert target.read_text(encoding="utf-8") == canonical
        return real_read(vault, path)

    monkeypatch.setattr(docs, "_project_current", tracked_project)
    monkeypatch.setattr(fp, "read_stable_markdown", tracked_read)

    report = docs.reindex_all()

    assert order[0] == "project"
    assert report["recovered_pending"] == 1
    assert report["updated"] == 0
    assert report["unchanged"] == 1
    assert report["skipped_conflicts"] == []
    assert docs.get("pending.md")["content"] == canonical
    assert target.read_text(encoding="utf-8") == canonical
    with ctx.db.reader() as conn:
        revisions = conn.execute(
            "SELECT version, op FROM revisions WHERE doc_id="
            "(SELECT id FROM documents WHERE path_norm=?) ORDER BY version",
            (path_norm("pending.md"),),
        ).fetchall()
    assert [(row["version"], row["op"]) for row in revisions] == [(1, "create")]


def test_new_target_insert_retries_when_managed_create_wins(
    ctx, principals, monkeypatch
):
    docs = ctx.docs
    editor = principals["editor"]
    target = ctx.settings.vault_path / "new-race.md"
    _write(target, "# External\n\nstale new file")

    thread, errors, reads = _run_after_first_stable_read(
        monkeypatch,
        lambda: docs.create(
            editor,
            "new-race.md",
            "# Managed\n\ncreate wins",
            embed=False,
        ),
    )

    report = docs.reindex_all()

    _join_race(thread, errors)
    assert len(reads) >= 2
    assert report["retried"] == 1
    assert report["created"] == 0
    assert report["unchanged"] == 1
    assert report["skipped_conflicts"] == []
    current = docs.get("new-race.md")
    assert current["version"] == 1
    assert current["content"] == "# Managed\n\ncreate wins"
    assert target.read_text(encoding="utf-8") == current["content"]
    with ctx.db.reader() as conn:
        rows = conn.execute(
            "SELECT r.version, r.op FROM revisions r JOIN documents d ON d.id=r.doc_id "
            "WHERE d.path_norm=? ORDER BY r.version",
            (path_norm("new-race.md"),),
        ).fetchall()
    assert [(row["version"], row["op"]) for row in rows] == [(1, "create")]


def test_three_unstable_file_generations_return_reason_without_stale_commit(
    ctx, monkeypatch
):
    docs = ctx.docs
    target = ctx.settings.vault_path / "flapping.md"
    _write(target, "# Generation 0\n\nexternal")
    real_read = fp.read_stable_markdown
    generations = 0

    def replace_after_read(vault: Path, path: Path):
        nonlocal generations
        stable = real_read(vault, path)
        generations += 1
        _replace(
            target,
            f"# Generation {generations}\n\nexternal",
            generation=generations,
        )
        return stable

    monkeypatch.setattr(fp, "read_stable_markdown", replace_after_read)

    report = docs.reindex_all()

    assert generations == 3
    assert report["retried"] == 2
    assert report["created"] == 0
    assert report["updated"] == 0
    assert report["unchanged"] == 0
    assert report["skipped_conflicts"] == [
        {"path": "flapping.md", "reason": "file_changed", "attempts": 3}
    ]
    with ctx.db.reader() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM documents WHERE path_norm=?",
            (path_norm("flapping.md"),),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE target=? AND action='doc_reconcile'",
            ("flapping.md",),
        ).fetchone()[0] == 0
