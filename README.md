# DistroHop

Toolkit distro-hopping untuk laptop dual-boot Windows + Linux: **backup → uninstall bersih → install distro baru → setup otomatis**, dengan Windows sebagai pusat kendali.

Masalah awalnya: setelah partisi Linux di-wipe, GRUB masih muncul saat boot karena folder bootloader di EFI System Partition (ESP), entri boot di NVRAM, dan partisi pendukung seperti `/boot` tidak ikut terhapus.

## Status: M0 + M1 (scan read-only)

Versi ini **hanya membaca**. Belum ada fitur yang mengubah apa pun.

| Milestone | Isi | Status |
|---|---|---|
| M0 | Fondasi: Runner, elevasi admin, logging, test | ✅ |
| M1a | Scan inventaris: disk/partisi, isi ESP, entri boot (`bcdedit`), BitLocker | ✅ |
| M1b | Scan analisis: NVRAM mentah, header filesystem (fsprobe), reference graph per instalasi | ✅ |
| M2 | Uninstall EFI (hapus folder & entri boot, dengan backup & pemulihan) | belum |
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
.venv\Scripts\python.exe -m distrohop

# Ringkasan di terminal + laporan JSON
.venv\Scripts\python.exe -m distrohop scan --json scan-output\laporan.scan.json

# Scan terbatas tanpa admin (hanya daftar partisi)
.venv\Scripts\python.exe -m distrohop --no-elevate scan
```

Tombol di dashboard: `r` scan ulang, `s` simpan laporan JSON ke `%LOCALAPPDATA%\DistroHop\reports\`, `q` keluar.
Log ada di `%LOCALAPPDATA%\DistroHop\logs\`.

## Test

```powershell
.venv\Scripts\python.exe -m pytest
```

Test tidak menyentuh sistem: perintah memakai `FakeRunner`, ESP memakai folder sementara, dan NVRAM/superblock dibuat sintetis. Inventaris disk di `tests/fixtures/disks_laptop.json` diambil dari laptop asli dengan nomor seri disamarkan.

## Struktur

```
src/distrohop/
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
