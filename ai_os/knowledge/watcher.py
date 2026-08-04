"""File watcher that keeps a `KnowledgeEngine` fresh as source files change
(the "Scope A" file-watcher — a pragmatic subset of doc 04 §3's event-driven
invalidation).

Deliberately a **zero-dependency polling watcher**, not an inotify/`watchdog`
one, matching this project's lean-footprint ethos (raw `httpx` over SDKs, etc.):
it snapshots each watched file's `(mtime_ns, size)` and, when any file is added,
removed, or changed, triggers a **full re-scan** via the existing
`CallGraphBuilder` + `KnowledgeEngine`. That full scan is only ~2s for ~350
files (see CLAUDE.md), so a debounced full rebuild is correct, simple, and far
cheaper than a genuinely-incremental single-file re-parse — which, because
CALLS/EXTENDS edges resolve against a project-wide name index, would need
careful cross-file edge fixup to stay correct (deliberately not built; see the
"file-watcher" note in CLAUDE.md / doc 04).

The core is synchronous and directly testable without any real event timing:
`start()` builds the baseline, then each `poll()` returns the `WatchEvent`
describing what changed (empty if nothing did) after rebuilding the engine.
`run()` is the blocking daemon loop the `ai-os watch` CLI drives.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

from ai_os.analyzer.call_graph_builder import CallGraphBuilder, iter_project_files
from ai_os.analyzer.call_graph_builder import DEFAULT_EXCLUDED_DIRS
from ai_os.analyzer.languages import detect_language
from ai_os.knowledge.graph_engine import KnowledgeEngine


@dataclass
class WatchEvent:
    """What changed between two polls, as repo-root-relative POSIX paths."""

    added: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.added or self.modified or self.removed)

    @property
    def total(self) -> int:
        return len(self.added) + len(self.modified) + len(self.removed)


class ProjectWatcher:
    """Polls a project root and rebuilds a `KnowledgeEngine` on any change.

    One instance owns the current engine (`self.engine`); `on_change` (if given)
    is called with `(engine, event)` after every rebuild so a caller can, e.g.,
    re-write `graph.json` or refresh a UI.
    """

    def __init__(
        self,
        root: Path,
        *,
        languages: Optional[set[str]] = None,
        extra_excluded_dirs: Iterable[str] = (),
        builder: Optional[CallGraphBuilder] = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.languages = languages
        self.extra_excluded_dirs = tuple(extra_excluded_dirs)
        self.builder = builder or CallGraphBuilder()
        self.engine = KnowledgeEngine()
        self._snapshot: dict[str, tuple[int, int]] = {}

    # -- snapshotting --------------------------------------------------------

    def _excluded(self) -> set[str]:
        return set(DEFAULT_EXCLUDED_DIRS) | set(self.extra_excluded_dirs)

    def _watched_files(self) -> Iterable[Path]:
        """Every file under the root that the analyzer would actually parse:
        respects the same excluded-dir set as `scan`, and keeps only files whose
        extension maps to a supported language (optionally narrowed to
        `self.languages`)."""
        for path in iter_project_files(self.root, self._excluded()):
            language = detect_language(path)
            if language is None:
                continue
            if self.languages is not None and language not in self.languages:
                continue
            yield path

    def _take_snapshot(self) -> dict[str, tuple[int, int]]:
        """`{relpath: (mtime_ns, size)}` for every watched file. Keying on both
        mtime and size catches same-second rewrites that don't move a
        coarse-resolution mtime."""
        snapshot: dict[str, tuple[int, int]] = {}
        for path in self._watched_files():
            try:
                st = path.stat()
            except OSError:
                # Raced away between listing and stat — treat as absent.
                continue
            relpath = path.relative_to(self.root).as_posix()
            snapshot[relpath] = (st.st_mtime_ns, st.st_size)
        return snapshot

    @staticmethod
    def _diff(
        old: dict[str, tuple[int, int]], new: dict[str, tuple[int, int]]
    ) -> WatchEvent:
        event = WatchEvent()
        for relpath, sig in new.items():
            if relpath not in old:
                event.added.append(relpath)
            elif old[relpath] != sig:
                event.modified.append(relpath)
        for relpath in old:
            if relpath not in new:
                event.removed.append(relpath)
        event.added.sort()
        event.modified.sort()
        event.removed.sort()
        return event

    # -- scanning ------------------------------------------------------------

    def _rescan(self) -> KnowledgeEngine:
        """Full scan + rebuild — the same code path `ai-os scan` uses."""
        result = self.builder.scan(
            self.root, languages=self.languages, extra_excluded_dirs=self.extra_excluded_dirs
        )
        engine = KnowledgeEngine()
        engine.build_from_scan(result)
        self.engine = engine
        return engine

    def start(self) -> KnowledgeEngine:
        """Do the initial scan and establish the baseline snapshot. Returns the
        freshly built engine."""
        engine = self._rescan()
        self._snapshot = self._take_snapshot()
        return engine

    def poll(self) -> WatchEvent:
        """One poll cycle: snapshot, and if anything changed since the last
        snapshot, rebuild the engine and return what changed (else an empty
        `WatchEvent`). Call `start()` once before the first `poll()`."""
        new_snapshot = self._take_snapshot()
        event = self._diff(self._snapshot, new_snapshot)
        if event:
            self._rescan()
            self._snapshot = new_snapshot
        return event

    def run(
        self,
        *,
        interval: float = 1.0,
        on_change: Optional[Callable[[KnowledgeEngine, WatchEvent], None]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
        sleep: Callable[[float], None] = time.sleep,
        max_cycles: Optional[int] = None,
    ) -> None:
        """Blocking daemon loop: `start()` once, then `poll()` every `interval`
        seconds, invoking `on_change(engine, event)` after each non-empty poll.

        `should_stop`/`max_cycles`/`sleep` are injection points so tests can run
        the loop deterministically without real waiting; the CLI just relies on
        `KeyboardInterrupt` to break out.
        """
        self.start()
        cycles = 0
        while True:
            if should_stop is not None and should_stop():
                return
            if max_cycles is not None and cycles >= max_cycles:
                return
            sleep(interval)
            event = self.poll()
            if event and on_change is not None:
                on_change(self.engine, event)
            cycles += 1
