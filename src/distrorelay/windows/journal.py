"""Journal eksekusi: setiap langkah dicatat 'started' sebelum dan 'done'/'failed' sesudah dijalankan.

Disimpan sebagai JSON Lines di %LOCALAPPDATA%\\DistroRelay\\journal\\<sesi>.jsonl, dan setiap baris di-fsync.
Kalau proses terhenti (crash, mati listrik), sesi tanpa 'session-end' terdeteksi saat DistroRelay dibuka lagi,
lengkap dengan langkah terakhir yang sempat dimulai.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, replace
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
        # Dua sesi dalam detik yang sama (mis. restore sesudah uninstall): beri akhiran, urutan nama tetap urutan waktu.
        name, counter = session, 1
        while (directory / f"{name}.jsonl").exists():
            counter += 1
            name = f"{session}_{counter}"  # "_" diurutkan sesudah ".", jadi tetap sesudah sesi aslinya
        journal = cls(directory / f"{name}.jsonl")
        journal._write(JournalEntry(_now(), "session-start", data=data))
        return journal

    def record(self, step: int, kind: str, ref: str | None, status: str, message: str = "") -> None:
        self._write(JournalEntry(_now(), status, step, kind, ref, message))

    def finish(self, ok: bool, message: str = "", *, changed: bool | None = None) -> None:
        """`changed=False` = pasti tidak ada yang diubah (mis. ESP gagal di-mount, script membatalkan diri)."""
        data = {"ok": ok} if changed is None else {"ok": ok, "changed": changed}
        self._write(JournalEntry(_now(), "session-end", message=message, data=data))

    def post_check(self, ok: bool, failed: list[str]) -> None:
        """Hasil scan ulang sesudah eksekusi. Hanya sesi dengan post-check hijau yang menuntaskan sesi terputus."""
        self._write(JournalEntry(_now(), "post-check", message=", ".join(failed), data={"ok": ok, "failed": failed}))

    def mark_resolved(self, note: str = "") -> None:
        """Pengguna menyatakan sesi ini sudah ditangani (kondisinya sudah dicek), tanpa sesi sejenis yang sukses."""
        self._write(JournalEntry(_now(), "resolved", message=note, data={"by": "pengguna"}))

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
    resolved_by: str | None = None  # sesi yang menuntaskannya, atau waktu ditandai tuntas oleh pengguna
    resolved_how: str | None = None  # "sesi" (sejenis, sukses) | "pemulihan" (restore yang mencakup) | "manual"
    end_message: str = ""
    kind: str = "uninstall"  # "uninstall" | "restore" | "shrink-c"; journal lama tanpa kind = uninstall
    changed: bool | None = None  # False = sesi selesai tanpa mengubah apa pun
    marked_resolved: JournalEntry | None = None  # entri "resolved" terakhir (ditandai tuntas oleh pengguna)
    touched: frozenset = frozenset()  # uninstall: item yang (mungkin) sudah dihapus, dalam bentuk _restorable_key
    irreversible: bool = False  # uninstall: C: sempat diperluas, jadi partisinya tidak bisa dipulihkan
    restored: frozenset = frozenset()  # restore: item yang dipulihkan sesi ini, dalam bentuk _restorable_key

    @property
    def left_partial_state(self) -> bool:
        """Terputus, dihentikan, gagal, atau post-check gagal: sistem mungkin setengah jadi."""
        if self.finished and self.changed is False:
            return False
        return not self.finished or not self.ok or self.post_check_ok is False

    @property
    def needs_attention(self) -> bool:
        return self.left_partial_state and self.resolved_by is None


# Langkah uninstall yang bisa dibatalkan lewat Pulihkan -> jenis item di daftar sesi restore.
_RESTORABLE = {"delete-esp-item": "esp", "delete-boot-entry": "entry", "delete-partition": "partition"}


def _restorable_key(kind: str | None, ref: str | None) -> tuple[str, str] | None:
    """('esp', 'efi\\fedora'), ('entry', 'boot0003'), ('partition', 'disk0-p5'); None untuk langkah lain."""
    group = _RESTORABLE.get(kind or "")
    if group is None or not ref:
        return None
    value = ref.lower()
    if group == "entry" and value.startswith("nv:"):  # ref komponen uninstall -> label Boot#### di sesi restore
        value = "boot" + value[3:]
    return group, value


def _restored_keys(data: dict) -> frozenset:
    return frozenset(
        [("esp", str(x).lower()) for x in data.get("esp_items") or []]
        + [("entry", str(x).lower()) for x in data.get("entries") or []]
        + [("partition", str(x).lower()) for x in data.get("partitions") or []]
    )


def _covers(restored: frozenset, touched: frozenset) -> bool:
    """Setiap item yang disentuh uninstall ada di daftar pemulihan (folder ESP boleh dipulihkan lewat induknya)."""
    def covered(group: str, value: str) -> bool:
        if group == "esp":
            return any(g == "esp" and (value == r or value.startswith(r + "\\")) for g, r in restored)
        return (group, value) in restored

    return all(covered(group, value) for group, value in touched)


def summarize(path: Path) -> SessionSummary:
    entries = read_journal(path)
    start = next((e for e in entries if e.status == "session-start"), None)
    end = next((e for e in reversed(entries) if e.status == "session-end"), None)
    check = next((e for e in reversed(entries) if e.status == "post-check"), None)
    steps = [e for e in entries if e.status in ("started", "done", "failed")]
    closed = {e.step for e in steps if e.status in ("done", "failed")}
    open_steps = [e for e in steps if e.status == "started" and e.step not in closed]
    kind = str((start.data or {}).get("kind") or "uninstall") if start else "uninstall"
    # Langkah yang sempat dimulai dianggap mungkin sudah mengubah sesuatu, juga yang gagal di tengah.
    touched = frozenset(key for e in steps if e.status == "started" and (key := _restorable_key(e.kind, e.ref)))
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
        kind=kind,
        changed=(end.data or {}).get("changed") if end else None,
        marked_resolved=next((e for e in reversed(entries) if e.status == "resolved"), None),
        touched=touched if kind == "uninstall" else frozenset(),
        irreversible=kind == "uninstall" and any(e.kind == "extend-c" for e in steps),
        restored=_restored_keys((start.data or {}) if start else {}) if kind == "restore" else frozenset(),
    )


def _succeeded(session: SessionSummary) -> bool:
    return session.finished and bool(session.ok) and bool(session.post_check_ok)


# path -> ((mtime, ukuran), ringkasan): file journal hanya bertambah, jadi yang tidak berubah tidak di-parse ulang.
_SUMMARIES: dict[Path, tuple[tuple[int, int], SessionSummary]] = {}


def _cached_summary(path: Path) -> SessionSummary:
    info = path.stat()
    key = (info.st_mtime_ns, info.st_size)
    cached = _SUMMARIES.get(path)
    if cached is None or cached[0] != key:
        cached = _SUMMARIES[path] = (key, summarize(path))
    return replace(cached[1])  # salinan: sessions() mengisi resolved_by per pemanggilan


def sessions(directory: Path | None = None) -> list[SessionSummary]:
    directory = directory or journal_dir()
    if not directory.exists():
        return []
    found = [_cached_summary(p) for p in sorted(directory.glob("*.jsonl"))]  # nama sesi = waktu mulai
    for n, session in enumerate(found):
        session.resolved_by = session.resolved_how = None
        if not session.left_partial_state:
            continue
        if session.marked_resolved is not None:
            session.resolved_by, session.resolved_how = session.marked_resolved.time, "manual"
            continue
        # Sesi sejenis yang sukses menuntaskan: post-check shrink-c tidak berkata apa pun tentang uninstall yang
        # terputus, dan sebaliknya.
        later = next((s for s in found[n + 1 :] if s.kind == session.kind and _succeeded(s)), None)
        if later is not None:
            session.resolved_by, session.resolved_how = later.path.stem, "sesi"
            continue
        # Uninstall yang semua item terhapusnya sudah dikembalikan oleh pemulihan yang sukses sesudahnya.
        if session.kind == "uninstall" and session.touched and not session.irreversible:
            restore = next(
                (s for s in found[n + 1 :] if s.kind == "restore" and _succeeded(s) and _covers(s.restored, session.touched)),
                None,
            )
            if restore is not None:
                session.resolved_by, session.resolved_how = restore.path.stem, "pemulihan"
    return found


def incomplete_sessions(directory: Path | None = None) -> list[SessionSummary]:
    """Sesi yang meninggalkan keadaan setengah jadi dan belum dituntaskan oleh sesi sesudahnya."""
    return [s for s in sessions(directory) if s.needs_attention]
