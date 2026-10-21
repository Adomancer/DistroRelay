"""Post-check setelah eksekusi: scan ulang, lalu bandingkan dengan kondisi yang diharapkan."""

from __future__ import annotations

from .backup import Verification
from .execute import BOOTMGFW, FALLBACK, manifest_hashes
from .scanner import ScanReport
from .uninstall_plan import UninstallPlan


def _under(path: str, prefixes: list[str]) -> bool:
    low = path.lower()
    return any(low == p or low.startswith(p + "\\") for p in prefixes)


def post_check(plan: UninstallPlan, manifest: dict, before: ScanReport, after: ScanReport) -> list[Verification]:
    checks: list[Verification] = []
    hashes = manifest_hashes(manifest)

    managers = [e for e in after.boot_entries if e.is_windows_boot_manager]
    first = bool(managers) and managers[0].order == 0
    checks.append(Verification("Windows Boot Manager urutan pertama", first,
                               "Ya" if first else "Tidak; lihat docs/RECOVERY.md bagian C"))

    if after.esp is None:
        checks.append(Verification("ESP terbaca", False, "ESP tidak bisa dibaca setelah eksekusi"))
        return checks
    bootmgfw = after.esp.find(BOOTMGFW)
    intact = bootmgfw is not None and bootmgfw.sha256 == hashes.get(BOOTMGFW.lower())
    checks.append(Verification("bootmgfw.efi utuh", intact, "Ada, hash sama dengan backup" if intact else "Hilang/berubah"))

    deleted_ids = {a.data["bcd_id"].lower() for a in plan.actions if a.kind == "delete-boot-entry"}
    deleted_targets = {
        ((a.data.get("description") or "").casefold(), (a.data.get("path") or "").lower())
        for a in plan.actions
        if a.kind == "delete-boot-entry"
    }
    remaining = [
        e
        for e in after.boot_entries
        if (e.bcd_id or "").lower() in deleted_ids or (e.description.casefold(), (e.path or "").lower()) in deleted_targets
    ]
    checks.append(Verification("Entri yang dihapus sudah tidak ada", not remaining,
                               "Ya" if not remaining else f"Masih ada: {', '.join(e.description for e in remaining)}"))

    kept_before = [
        e
        for e in before.boot_entries
        if (e.bcd_id or "").lower() not in deleted_ids
        and (e.description.casefold(), (e.path or "").lower()) not in deleted_targets
    ]
    after_keys = {((e.description or "").casefold(), (e.path or "").lower()) for e in after.boot_entries}
    lost = [e for e in kept_before if ((e.description or "").casefold(), (e.path or "").lower()) not in after_keys]
    checks.append(Verification("Entri lain tetap ada", not lost,
                               "Ya" if not lost else f"Hilang: {', '.join(e.description for e in lost)}"))

    deleted_paths = [a.data["path"].lower() for a in plan.actions if a.kind == "delete-esp-item"]
    leftovers = [p for p in deleted_paths if after.esp.find(p) or after.esp.has_dir(p)]
    checks.append(Verification("File/folder yang dihapus sudah tidak ada", not leftovers,
                               "Ya" if not leftovers else f"Masih ada: {', '.join(leftovers)}"))

    # Selalu dicek, juga kalau fallback sudah diganti oleh sesi sebelumnya yang terputus: kalau NVRAM ter-reset,
    # firmware menjalankan file ini (skenario VM 10).
    restored_fallback = any(a.kind == "restore-fallback" for a in plan.actions)
    fallback = after.esp.find(FALLBACK)
    windows_fallback = fallback is not None and bootmgfw is not None and fallback.sha256 == bootmgfw.sha256
    deleted_twins = sorted(
        path for path, digest in hashes.items()
        if fallback is not None and digest == fallback.sha256 and _under(path, deleted_paths)
    )
    if restored_fallback or deleted_twins:
        detail = "EFI\\Boot\\bootx64.efi identik dengan bootmgfw.efi"
        if not windows_fallback:
            detail = f"Tidak; isinya sama dengan file yang sudah dihapus ({deleted_twins[0]})" if deleted_twins else "Tidak identik"
        checks.append(Verification("Fallback milik Windows", windows_fallback, detail))
    else:
        detail = (
            "EFI\\Boot\\bootx64.efi identik dengan bootmgfw.efi" if windows_fallback
            else "Tidak ada" if fallback is None
            else "Bukan milik Windows, tapi tidak sama dengan file yang dihapus"
        )
        checks.append(Verification("Fallback firmware", True, detail))

    current = {f.path.lower(): f.sha256 for f in after.esp.files}
    expected = {
        path: digest
        for path, digest in hashes.items()
        if not _under(path, deleted_paths) and not (restored_fallback and path == FALLBACK.lower())
    }
    changed = sorted(p for p, digest in expected.items() if current.get(p) != digest)
    added = sorted(p for p in current if p not in hashes and not (restored_fallback and p == FALLBACK.lower()))
    untouched = not changed and not added
    detail = "Semua file lain sama dengan backup"
    if not untouched:
        detail = f"Berubah/hilang: {changed[:3]}; baru: {added[:3]}"
    checks.append(Verification("File ESP lain tidak tersentuh", untouched, detail))

    layout_same = before.inventory is not None and after.inventory is not None and (
        before.inventory.fingerprint() == after.inventory.fingerprint()
    )
    checks.append(Verification("Layout disk tidak berubah", layout_same, "Ya" if layout_same else "Berubah!"))
    return checks
