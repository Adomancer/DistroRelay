"""Journal eksekusi: setiap langkah dicatat 'started' sebelum dan 'done'/'failed' sesudah dijalankan.

Disimpan sebagai JSON Lines di %LOCALAPPDATA%\\DistroRelay\\journal\\<sesi>.jsonl, dan setiap baris di-fsync.
Kalau proses terhenti (crash, mati listrik), sesi tanpa 'session-end' terdeteksi saat DistroRelay dibuka lagi,
lengkap dengan langkah terakhir yang sempat dimulai.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from ..paths import app_data_dir


def journal_dir() -> Path:
    return app_data_dir() / "journal"


@dataclass
class JournalEntry:
    time: str
    status: str  # "session-start" | "started" | "done" | "failed" | "session-end"
    step: int | None = None
    kind: str | None = None
    ref: str | None = None
    message: str = ""
    data: dict | None = None


class Journal:
    def __init__(self, path: Path):
        self.path = Path(path)

    @classmethod
    def start(cls, directory: Path, session: str, data: dict) -> "Journal":
        directory.mkdir(parents=True, exist_ok=True)
        journal = cls(directory / f"{session}.jsonl")
        if journal.path.exists():
            raise FileExistsError(f"Journal {journal.path} sudah ada")
        journal._write(JournalEntry(_now(), "session-start", data=data))
        return journal

    def record(self, step: int, kind: str, ref: str | None, status: str, message: str = "") -> None:
        self._write(JournalEntry(_now(), status, step, kind, ref, message))

    def finish(self, ok: bool, message: str = "") -> None:
        self._write(JournalEntry(_now(), "session-end", message=message, data={"ok": ok}))

    def post_check(self, ok: bool, failed: list[str]) -> None:
        """Hasil scan ulang sesudah eksekusi. Hanya sesi dengan post-check hijau yang menuntaskan sesi terputus."""
        self._write(JournalEntry(_now(), "post-check", message=", ".join(failed), data={"ok": ok, "failed": failed}))

    def _write(self, entry: JournalEntry) -> None:
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def read_journal(path: Path) -> list[JournalEntry]:
    entries = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                entries.append(JournalEntry(**json.loads(line)))
            except (ValueError, TypeError):
                entries.append(JournalEntry("", "rusak", message=line[:200]))
    return entries


@dataclass
class SessionSummary:
    path: Path
    started: str
    finished: bool
    ok: bool | None
    last_step: JournalEntry | None  # langkah terakhir yang tercatat (started/done/failed)
    unfinished_step: JournalEntry | None  # 'started' tanpa 'done'/'failed' = terputus di tengah langkah ini
    backup: str | None = None  # ID backup yang dipakai sesi ini
    post_check_ok: bool | None = None  # None = post-check tidak tercatat
    resolved_by: str | None = None  # sesi sesudahnya yang selesai dengan post-check hijau
    end_message: str = ""

    @property
    def left_partial_state(self) -> bool:
        """Terputus, dihentikan, gagal, atau post-check gagal: sistem mungkin setengah jadi."""
        return not self.finished or not self.ok or self.post_check_ok is False

    @property
    def needs_attention(self) -> bool:
        return self.left_partial_state and self.resolved_by is None


def summarize(path: Path) -> SessionSummary:
    entries = read_journal(path)
    start = next((e for e in entries if e.status == "session-start"), None)
    end = next((e for e in reversed(entries) if e.status == "session-end"), None)
    check = next((e for e in reversed(entries) if e.status == "post-check"), None)
    steps = [e for e in entries if e.status in ("started", "done", "failed")]
    closed = {e.step for e in steps if e.status in ("done", "failed")}
    open_steps = [e for e in steps if e.status == "started" and e.step not in closed]
    return SessionSummary(
        path=Path(path),
        started=start.time if start else "",
        finished=end is not None,
        ok=(end.data or {}).get("ok") if end else None,
        last_step=steps[-1] if steps else None,
        unfinished_step=open_steps[-1] if open_steps else None,
        backup=(start.data or {}).get("backup") if start else None,
        post_check_ok=(check.data or {}).get("ok") if check else None,
        end_message=end.message if end else "",
    )


def sessions(directory: Path | None = None) -> list[SessionSummary]:
    directory = directory or journal_dir()
    if not directory.exists():
        return []
    found = [summarize(p) for p in sorted(directory.glob("*.jsonl"))]  # nama sesi = waktu mulai
    for n, session in enumerate(found):
        if not session.left_partial_state:
            continue
        later = next((s for s in found[n + 1 :] if s.finished and s.ok and s.post_check_ok), None)
        session.resolved_by = later.path.stem if later else None
    return found


def incomplete_sessions(directory: Path | None = None) -> list[SessionSummary]:
    """Sesi yang meninggalkan keadaan setengah jadi dan belum dituntaskan oleh sesi sesudahnya."""
    return [s for s in sessions(directory) if s.needs_attention]
