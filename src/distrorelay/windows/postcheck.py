"""Post-check setelah eksekusi: scan ulang, lalu bandingkan dengan kondisi yang diharapkan."""

from __future__ import annotations

from collections import Counter

from .backup import VOLATILE_ESP_FILES, Verification
from .execute import BOOTMGFW, FALLBACK, manifest_hashes, relevant_disks
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

    def entry_key(description: str | None, path: str | None) -> tuple[str, str]:
        return ((description or "").casefold(), (path or "").lower())

    # Entri dobel (nama + path sama) dihitung per jumlah: menghapus satu dari dua entri kembar meninggalkan satu.
    deleted = [a for a in plan.actions if a.kind == "delete-boot-entry"]
    deleted_ids = {a.data["bcd_id"].lower() for a in deleted}
    deleted_counts = Counter(entry_key(a.data.get("description"), a.data.get("path")) for a in deleted)
    before_counts = Counter(entry_key(e.description, e.path) for e in before.boot_entries)
    after_counts = Counter(entry_key(e.description, e.path) for e in after.boot_entries)
    remaining = [e.description for e in after.boot_entries if (e.bcd_id or "").lower() in deleted_ids]
    remaining += [key[0] for key, n in deleted_counts.items() if after_counts[key] > max(0, before_counts[key] - n)]
    checks.append(Verification("Entri yang dihapus sudah tidak ada", not remaining,
                               "Ya" if not remaining else f"Masih ada: {', '.join(sorted(set(remaining)))}"))

    lost = [
        key[0] or key[1]
        for key, n in before_counts.items()
        if after_counts[key] < n - deleted_counts.get(key, 0)
    ]
    checks.append(Verification("Entri lain tetap ada", not lost,
                               "Ya" if not lost else f"Hilang: {', '.join(lost)}"))

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
    # bootstat.dat ditulis Windows setiap boot: reboot di antara backup dan eksekusi bukan "file lain tersentuh"
    # (uji VM U-4 2026-10-25: post-check gagal hanya karena file ini, padahal gerbang sudah mengabaikannya).
    changed = sorted(p for p, digest in expected.items() if current.get(p) != digest and p not in VOLATILE_ESP_FILES)
    added = sorted(p for p in current if p not in hashes and p not in VOLATILE_ESP_FILES
                   and not (restored_fallback and p == FALLBACK.lower()))
    untouched = not changed and not added
    detail = "Semua file lain sama dengan backup"
    if not untouched:
        detail = f"Berubah/hilang: {changed[:3]}; baru: {added[:3]}"
    checks.append(Verification("File ESP lain tidak tersentuh", untouched, detail))

    checks.extend(_layout_checks(plan, before, after))
    return checks


def _layout_checks(plan: UninstallPlan, before: ScanReport, after: ScanReport) -> list[Verification]:
    """Partisi yang dihapus hilang, C: bertambah kalau diperluas, dan semua partisi lain sama persis."""
    deleted = {a.data["guid"]: a.data for a in plan.actions if a.kind == "delete-partition"}
    extended = {a.data["guid"]: a.data for a in plan.actions if a.kind == "extend-c"}
    if before.inventory is None or after.inventory is None:
        return [Verification("Layout disk sesuai rencana", False, "Daftar disk tidak terbaca")]
    checks = []
    if deleted:
        still = [d["id"] for guid, d in deleted.items() if after.inventory.by_guid(guid) is not None]
        checks.append(Verification("Partisi yang dihapus sudah tidak ada", not still,
                                   "Ya" if not still else f"Masih ada: {', '.join(still)}"))
    for guid, data in extended.items():
        grown = after.inventory.by_guid(guid)
        ok = grown is not None and grown.size > data["size"]
        checks.append(Verification("C: diperluas", ok, f"{data['size']:,} → {grown.size:,} byte" if ok else "Tidak bertambah"))
    skip = set(deleted) | set(extended)
    # Flashdisk/HDD yang dicolok atau dicabut sesudah scan tidak membuat post-check gagal.
    relevant = relevant_disks(before.inventory, plan)
    before_layout = before.inventory.layout() if relevant is None else before.inventory.layout_of(relevant)
    after_layout = after.inventory.layout() if relevant is None else after.inventory.layout_of(relevant)
    expected = {item for item in before_layout if item[1] not in skip}
    current = {item for item in after_layout if item[1] not in extended}
    same = current == expected
    title = "Layout disk lain tidak berubah" if skip else "Layout disk tidak berubah"
    checks.append(Verification(title, same, "Ya" if same else "Berubah!"))
    return checks
