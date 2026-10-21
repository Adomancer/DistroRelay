# DistroRelay

Toolkit distro-hopping untuk laptop dual-boot Windows + Linux: **backup → uninstall bersih → install distro baru → setup otomatis**, dengan Windows sebagai pusat kendali.

Masalah awalnya: setelah partisi Linux di-wipe, GRUB masih muncul saat boot karena folder bootloader di EFI System Partition (ESP), entri boot di NVRAM, dan partisi pendukung seperti `/boot` tidak ikut terhapus.

## Status: M0 + M1 + M2a + M2b (CLI)

Scan, dry-run, dan backup tidak mengubah sistem. **`uninstall` menghapus entri boot dan folder ESP.**
Jalankan perintah itu **di VM dulu** (lihat [tests/vm/SCENARIOS.md](tests/vm/SCENARIOS.md)). Di laptop asli, `uninstall` menolak jalan
kecuali ada backup dengan salinan terverifikasi di drive eksternal, USB darurat sudah dicoba, dan kamu mengonfirmasi bahwa test di VM sudah lolos.

| Milestone | Isi | Status |
|---|---|---|
| M0 | Fondasi: Runner, elevasi admin, logging, test | ✅ |
| M1a | Scan inventaris: disk/partisi, isi ESP, entri boot (`bcdedit`), BitLocker | ✅ |
| M1b | Scan analisis: NVRAM mentah, header filesystem (fsprobe), reference graph per instalasi | ✅ |
| M2a | Preflight, backup per lapisan + verifikasi + salinan USB, dry-run rencana uninstall, `docs/RECOVERY.md` | ✅ |
| M2b | Eksekusi uninstall EFI (journal, post-check, `restore`), CLI | ✅ (belum dites di VM) |
| M2c | Wizard uninstall di TUI | belum |
| M3 | Hapus partisi Linux | belum |
| M4–M7 | Profil + setup Linux, backup Linux, ISO manager, rilis `.exe` | belum |

## Apa yang dilakukan scan

| Langkah | Cara | Butuh admin |
|---|---|---|
| Daftar disk & partisi | `Get-Disk`, `Get-Partition` | tidak |
| Header filesystem partisi Linux | baca 128 KiB awal partisi dari `\\.\PhysicalDriveN` (mode `rb`) | ya |
| Entri boot | `bcdedit /enum firmware /v` + baca NVRAM langsung (`GetFirmwareEnvironmentVariableW`) | ya |
| Isi ESP | `mountvol X: /S` → data semua file + SHA256 → `mountvol X: /D` | ya |
| Status BitLocker | WMI `Win32_EncryptableVolume` | ya |

Pengaman yang dipasang:
- Semua perintah lewat `ReadOnlyRunner`, yang **menolak** perintah apa pun di luar daftar read-only (mis. `bcdedit /delete`, `Remove-Partition`, `diskpart`). Satu-satunya pengecualian adalah mount ESP sementara, yang selalu di-unmount lagi.
- Modul NVRAM tidak punya fungsi tulis. Disk mentah hanya dibuka untuk dibaca.
- Kesimpulan seperti "kemungkinan sisa" selalu disertai alasan. Pola nama file tidak pernah dipakai sebagai bukti.
- Di laporan JSON dan output mentah (`--raw-dir`), nomor seri disk serta identitas perangkat di NVRAM (nomor seri SSD, EUI NVMe, MAC) disamarkan. Nama file di ESP tetap tercantum apa adanya, termasuk file pabrikan yang namanya bisa memuat nomor seri laptop, jadi periksa dulu sebelum laporan dibagikan ke publik.

## Cara menjalankan

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"

# Dashboard (otomatis meminta hak Administrator lewat UAC)
.venv\Scripts\python.exe -m distrorelay

# Ringkasan di terminal + laporan JSON
.venv\Scripts\python.exe -m distrorelay scan --json scan-output\laporan.scan.json

# Scan terbatas tanpa admin (hanya daftar partisi)
.venv\Scripts\python.exe -m distrorelay --no-elevate scan

# Dry-run rencana uninstall: tanpa --select tampilkan kandidat, dengan --select tampilkan urutan & perintah persis
.venv\Scripts\python.exe -m distrorelay plan
.venv\Scripts\python.exe -m distrorelay plan --select efi:fedora --select efi:arch

# Preflight + backup per lapisan ke %LOCALAPPDATA%\DistroRelay\backups\ dan drive eksternal
# (flashdisk atau HDD eksternal, disk fisik selain SSD Windows), lalu verifikasi
.venv\Scripts\python.exe -m distrorelay backup --drive E

# Mode coba tanpa USB (backup hanya di C:; belum memenuhi syarat eksekusi M2b)
.venv\Scripts\python.exe -m distrorelay backup --local-only

# Verifikasi ulang folder backup kapan saja (tidak butuh admin)
.venv\Scripts\python.exe -m distrorelay verify-backup E:\DistroRelay\backups\<id>

# --- Mengubah sistem: coba di VM dulu ---
# Jalankan rencana (banyak konfirmasi, terakhir ketik HAPUS).
# Ctrl+C saat berjalan = selesaikan langkah yang sedang jalan, lalu berhenti dengan rapi.
# Mode "vm" hanya berlaku kalau komputernya memang terdeteksi VM; di mesin fisik selalu dianggap laptop asli.
.venv\Scripts\python.exe -m distrorelay uninstall --select efi:fedora --backup <id>
# Pulihkan folder ESP / entri boot dari backup
.venv\Scripts\python.exe -m distrorelay restore --backup <id> --esp-item EFI\fedora --boot-entry Fedora
# Lihat sesi eksekusi, termasuk yang terputus di tengah
.venv\Scripts\python.exe -m distrorelay journal
```

Rencana uninstall selalu berurutan supaya laptop tetap bisa boot ke Windows di setiap titik:
Windows Boot Manager jadi pertama → fallback `EFI\Boot\bootx64.efi` dikembalikan ke milik Windows (tanda tangan Microsoft dicek dulu) → entri boot dihapus → folder ESP dihapus. Prosedur darurat ada di [docs/RECOVERY.md](docs/RECOVERY.md).

Tombol di dashboard: `r` scan ulang, `s` simpan laporan JSON ke `%LOCALAPPDATA%\DistroRelay\reports\`, `q` keluar.
Log ada di `%LOCALAPPDATA%\DistroRelay\logs\`.

## Test

```powershell
.venv\Scripts\python.exe -m pytest
```

Test tidak menyentuh sistem: perintah memakai `FakeRunner`, ESP memakai folder sementara, dan NVRAM/superblock dibuat sintetis. Inventaris disk di `tests/fixtures/disks_laptop.json` diambil dari laptop asli dengan nomor seri disamarkan.

## Struktur

```
src/distrorelay/
├─ __main__.py            CLI & elevasi admin
├─ render.py              tampilan bersama untuk CLI dan TUI
├─ tui/app.py             dashboard Textual
└─ windows/
   ├─ runner.py           Runner, FakeRunner, ReadOnlyRunner
   ├─ disks.py            inventaris disk/partisi
   ├─ gpt_types.py        tabel tipe partisi GPT (whitelist Linux)
   ├─ esp.py              mount & data isi ESP
   ├─ bootentries.py      parse bcdedit + gabung dengan NVRAM
   ├─ nvram.py            baca & parse EFI_LOAD_OPTION
   ├─ fsprobe.py          kenali filesystem dari superblock
   ├─ bootrefs.py         reference graph & kesimpulan per instalasi
   ├─ bitlocker.py        status BitLocker
   └─ scanner.py          orkestrasi scan & laporan JSON
```
