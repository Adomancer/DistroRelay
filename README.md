# DistroRelay

Toolkit distro-hopping untuk laptop dual-boot Windows + Linux: **backup → uninstall bersih → install distro baru → setup otomatis**, dengan Windows sebagai pusat kendali.

Masalah awalnya: setelah partisi Linux di-wipe, GRUB masih muncul saat boot karena folder bootloader di EFI System Partition (ESP), entri boot di NVRAM, dan partisi pendukung seperti `/boot` tidak ikut terhapus.

## Status: M0 + M1 + M2 + M3 (CLI + wizard TUI)

Scan, dry-run, dan backup tidak mengubah sistem. **`uninstall` (juga wizard uninstall di TUI) menghapus entri boot, folder ESP, dan partisi Linux yang dipilih.**
Jalankan **di VM dulu** (lihat [tests/vm/SCENARIOS.md](tests/vm/SCENARIOS.md)). Di laptop asli, `uninstall` menolak jalan
kecuali ada backup dengan salinan terverifikasi di drive eksternal, USB darurat sudah dicoba, dan kamu mengonfirmasi bahwa test di VM sudah lolos.

| Milestone | Isi | Status |
|---|---|---|
| M0 | Fondasi: Runner, elevasi admin, logging, test | ✅ |
| M1a | Scan inventaris: disk/partisi, isi ESP, entri boot (`bcdedit`), BitLocker | ✅ |
| M1b | Scan analisis: NVRAM mentah, header filesystem (fsprobe), reference graph per instalasi | ✅ |
| M2a | Preflight, backup per lapisan + verifikasi + salinan USB, dry-run rencana uninstall, `docs/RECOVERY.md` | ✅ |
| M2b | Eksekusi uninstall EFI (journal, post-check, `restore`), CLI | ✅ (lolos skenario VM) |
| M2c | Alur tunggal `UninstallFlow`/`RestoreFlow`; wizard uninstall, menu Pulihkan, dan journal di TUI | ✅ fase A + B lolos VM (sisa: cek 2× reboot skenario 9) |
| M3 | Hapus partisi Linux (pengaman + ketik ID partisi, aturan backup bertingkat), pulihkan partisi, perluas C: kalau bersebelahan | ✅ kode + test simulasi; skenario VM belum |
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
# Key instalasi (efi:fedora) = semua komponennya, termasuk partisinya: ID partisi (mis. disk0-p5) wajib diketik,
# dan partisi yang mungkin berisi data (root/home/...) juga wajib pernyataan "DATA SUDAH DIBACKUP".
.venv\Scripts\python.exe -m distrorelay uninstall --select efi:fedora --backup <id>
# Hanya bagian EFI-nya (tanpa partisi): pilih per komponen; ref-nya terlihat di `plan`
.venv\Scripts\python.exe -m distrorelay uninstall --select EFI\fedora --select nv:0003 --backup <id>
# Perluas C: ke ruang bekas partisi (hanya kalau partisinya tepat sesudah C:; sesudahnya tidak bisa dipulihkan)
.venv\Scripts\python.exe -m distrorelay uninstall --select efi:fedora --extend-c --backup <id>
# Pulihkan partisi / folder ESP / entri boot dari backup
.venv\Scripts\python.exe -m distrorelay restore --backup <id> --partition disk0-p5 --esp-item EFI\fedora --boot-entry Fedora
# Lihat sesi eksekusi, termasuk yang terputus di tengah
.venv\Scripts\python.exe -m distrorelay journal
```

Rencana uninstall selalu berurutan supaya laptop tetap bisa boot ke Windows di setiap titik:
Windows Boot Manager jadi pertama → fallback `EFI\Boot\bootx64.efi` dikembalikan ke milik Windows (tanda tangan Microsoft dicek dulu) → entri boot dihapus → folder ESP dihapus → partisi dihapus (layout disk dicek ulang tepat sebelumnya; hanya script persis
dari rencana yang boleh memanggil `Remove-Partition`) → (opsional) C: diperluas. Ruang bekas partisi dibiarkan unallocated
kecuali kamu memilih perluas C:. Prosedur darurat ada di [docs/RECOVERY.md](docs/RECOVERY.md).

Tombol di dashboard: `r` scan ulang, `s` simpan laporan JSON ke `%LOCALAPPDATA%\DistroRelay\reports\`, `u` wizard uninstall,
`p` Pulihkan, `j` journal, `q` keluar. Dashboard sendiri read-only. Banner kuning muncul kalau ada sesi uninstall yang belum tuntas.

Wizard uninstall (`u`) memakai alur yang sama dengan CLI `uninstall`: sesi belum tuntas (ketik `LANJUT`) → mode VM/asli →
scan + pilih kandidat (spasi = centang, rencana dry-run tampil di sebelahnya) → preflight → backup baru atau pakai yang ada →
dry-run + gerbang + ketik `HAPUS` → eksekusi + post-check. Selama eksekusi, Ctrl+C / Esc / Ctrl+Q = berhenti dengan rapi
setelah langkah yang sedang jalan. Menu Pulihkan (`p`): pilih backup → centang folder ESP / entri boot → ketik `PULIHKAN`.
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
├─ tui/                   dashboard + wizard uninstall, Pulihkan, journal (Textual; hanya lewat workflow)
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
   ├─ scanner.py          orkestrasi scan & laporan JSON
   ├─ machine.py          deteksi VM atau mesin fisik
   ├─ preflight.py        cek sebelum backup & eksekusi
   ├─ backup.py           backup per lapisan + verifikasi
   ├─ uninstall_plan.py   rencana uninstall (dry-run)
   ├─ execute.py          eksekusi + gerbang
   ├─ journal.py          journal eksekusi
   ├─ postcheck.py        post-check sesudah eksekusi
   ├─ restore.py          pemulihan folder ESP & entri boot
   ├─ stopping.py         berhenti rapi (Ctrl+C, jendela ditutup)
   └─ workflow.py         UninstallFlow & RestoreFlow: satu-satunya jalan ke perubahan sistem
```
