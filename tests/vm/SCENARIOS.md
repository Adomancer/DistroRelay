# Skenario test di VM UEFI

Wajib lolos sebelum M2b/M3 dijalankan di laptop asli. Firmware VM tidak sama dengan firmware laptop,
jadi VM menurunkan risiko tapi tidak menghapusnya: dry-run + post-check di laptop asli tetap wajib.

## Persiapan

- Hypervisor: **VirtualBox 7** (EFI, NVRAM persisten, vTPM, snapshot) atau **VMware Workstation Pro**
  (gratis untuk pemakaian personal; biasanya lebih mulus kalau WSL2 aktif). Hyper-V tidak ada di Windows Home.
- Ruang kosong ±60–80 GB (disk dinamis). Windows di VM tidak perlu diaktivasi.
- Snapshot dasar:
  - `dualboot-grub`: Windows + Fedora (GRUB + shim)
  - `dualboot-sdboot`: Windows + Arch (systemd-boot, kernel di ESP)
  - `dualboot-dua-linux`: Windows + 2 distro
- **Cek dulu:** ubah urutan boot → restore snapshot → urutan harus kembali. Kalau tidak kembali, berarti snapshot
  tidak menyimpan NVRAM, jadi backup file `.nvram` VM secara manual sebelum tiap skenario.
- VirtualBox sangat lambat kalau Hyper-V aktif di host (WSL2, Memory Integrity): `VBox.log` berisi
  `fall back to NEM`. Matikan dengan `bcdedit /set hypervisorlaunchtype off` + restart (WSL2 tidak jalan sampai
  dikembalikan dengan `bcdedit /set hypervisorlaunchtype auto`), atau pakai VMware.

## Jalur cepat: VM Windows saja + sisa Linux palsu

Tanpa install Linux. [`fake_leftovers.py`](fake_leftovers.py) meniru kondisi laptop asli di dalam VM: partisi #5
XBOOTLDR dengan header ext4 `/boot`, `EFI\fedora`, `EFI\Arch`, `grub\` yatim, `EFI\HP`, fallback berisi shim,
serta entri "Fedora" dan "Arch" di urutan pertama. Script ini menolak jalan di luar VirtualBox/VMware.

```powershell
# Di VM (PowerShell Administrator, folder repo). Disk kedua yang masih kosong ikut di-format jadi E:.
.venv\Scripts\python.exe tests\vm\fake_leftovers.py
# Matikan VM → snapshot "sisa-palsu" → nyalakan, lalu jalankan scan/plan/backup/uninstall seperti di bawah
```

Isi `.efi`-nya palsu, jadi firmware gagal menjalankan entri Fedora/Arch lalu lanjut ke Windows Boot Manager.
Kalau VM berhenti di layar firmware, tekan F2 → Boot Manager → Windows Boot Manager.

Yang **tidak** teruji di jalur ini: skenario 1, 5, 6, dan bagian "Linux bisa boot lagi" di skenario 9. Itu butuh
Linux sungguhan, jadi wajib sebelum menghapus Linux yang **masih terpasang**, bukan untuk membersihkan sisa.

## Skenario

Kolom "Lolos" mencatat versi kode saat skenario itu lolos. Setiap perubahan di jalur eksekusi (mis. refactor
`UninstallFlow` di M2c fase A) berarti skenario 2, 3, 7, 7b, 7c, 9, dan 10 **diulang dengan kode terbaru** sebelum
laptop asli. Skenario 8 juga, karena prosedurnya memakai folder `esp\` hasil backup.

| # | Skenario | Yang dicek | Untuk | Lolos |
|---|---|---|---|---|
| 1 | Uninstall Linux yang masih utuh (Fedora + GRUB) | VM langsung boot ke Windows, post-check hijau | M2b, M3 | [ ] |
| 2 | Kondisi seperti laptop asli: root sudah di-wipe, `/boot` + folder ESP + entri tersisa | Terdeteksi "kemungkinan sisa" dengan alasan benar; setelah dibersihkan langsung boot ke Windows | M1b, M2b | [x] fase A (UninstallFlow), 2026-10-22, sisa palsu |
| 3 | Urutan boot Linux duluan | `SetWindowsFirst` jalan pertama dan benar | M2b | [x] fase A (UninstallFlow), 2026-10-22, sisa palsu |
| 4 | Entri merujuk `.efi` yang hilang, atau folder tanpa entri | Status yatim / tidak pasti benar | M1b, M2b | [ ] |
| 5 | Arch + systemd-boot (kernel, `loader/`, UKI di ESP) | File dikelompokkan ke instalasi yang benar | M1b, M2b | [ ] |
| 6 | Dua Linux terinstall, hapus satu | Milik distro lain tidak tersentuh, distro itu masih bisa boot | M2b, M3 | [ ] |
| 7 | Gagal di tengah (`DISTRORELAY_FAIL_AFTER=<aksi>`, build dev) + VM dimatikan paksa | Journal mendeteksi, post-check & Pulihkan bekerja, Windows tetap boot | M2b | [x] fase A (UninstallFlow), 2026-10-22: crash setelah langkah 1 + mati paksa → Arch tidak kembali, TERPUTUS, dituntaskan dengan backup baru, post-check 8/8 |
| 7b | Ctrl+C saat `uninstall` sedang jalan | Langkah yang sedang jalan selesai dulu, lalu berhenti; journal "dihentikan"; `uninstall` berikutnya minta LANJUT; Windows tetap boot | M2c | [x] fase A (UninstallFlow), 2026-10-22: perintah diketik langsung → langkah 2 diselesaikan lalu berhenti, journal "dihentikan". Catatan: lewat pipeline/script PowerShell, Ctrl+C MEMATIKAN proses (journal TERPUTUS) |
| 7c | Jendela PowerShell ditutup (X) saat `uninstall` sedang jalan | Sama dengan 7b sejauh Windows memberi waktu (±5 detik); kalau tidak sempat, journal TERPUTUS | M2c | [x] fase A (UninstallFlow), 2026-10-22: Alt+F4 saat langkah 2 → langkah selesai, journal "dihentikan" tertulis sebelum proses ditutup; ESP tertinggal ter-mount di Y: dan dipakai ulang di run berikutnya |
| 8 | Boot rusak sengaja (`EFI\Microsoft` dihapus di VM) | `docs/RECOVERY.md` berhasil sampai Windows boot lagi (**latihan untuk kamu**) | M2b | [ ] |
| 9 | `RestoreEspFiles` + `RestoreBootEntry` + `RestorePartition` setelah skenario 1 | Linux bisa boot lagi; setelah 2× reboot entri yang dipulihkan tidak jadi dobel (di VirtualBox, entri buatan `bcdedit /copy` pernah dibuat ulang tiap boot) | M2b, M3 | [~] fase A (UninstallFlow), 2026-10-22: EFI\Arch + EFI\fedora + entri (nama & Boot####) dipulihkan, stabil 2× reboot (1 paksa); nama dobel ditolak dengan pilihan nomor. Linux boot & partisi belum |
| 10 | NVRAM di-reset (hapus file `.nvram` VM) setelah uninstall | Boot ke Windows lewat fallback, dengan dan tanpa `RestoreFallbackToWindows` | M2b | [x] fase A (UninstallFlow), 2026-10-22: semua Boot#### dihapus → desktop Windows lewat fallback |

## Cara menjalankan di VM (M2b)

Di dalam VM Windows (dual-boot dengan Linux), clone repo, buat venv, lalu:

```powershell
# 1. Backup dengan salinan ke drive eksternal (di VM: virtual disk kedua, misal E:)
.venv\Scripts\python.exe -m distrorelay backup --drive E

# 2. Lihat kandidat & rencana
.venv\Scripts\python.exe -m distrorelay plan
.venv\Scripts\python.exe -m distrorelay plan --select efi:fedora

# 3. Jalankan (jawab "vm" saat ditanya)
.venv\Scripts\python.exe -m distrorelay uninstall --select efi:fedora --backup <ID>

# Skenario 7 (crash di tengah): hanya aktif kalau dua variabel ini di-set
$env:DISTRORELAY_DEV = "1"; $env:DISTRORELAY_FAIL_AFTER = "3"
.venv\Scripts\python.exe -m distrorelay uninstall --select efi:fedora --backup <ID>
.venv\Scripts\python.exe -m distrorelay journal

# Skenario 9 (pulihkan)
.venv\Scripts\python.exe -m distrorelay restore --backup <ID> --esp-item EFI\fedora --boot-entry Fedora
```

## Gate M2c fase B: lewat wizard TUI

Wizard memakai `UninstallFlow`/`RestoreFlow` yang sama dengan CLI. Bedanya ada di cara input dan cara berhenti:
Textual mematikan *processed input* di konsol, jadi Ctrl+C sampai sebagai tombol, bukan sinyal. Ulangi dari snapshot
`siap-uninstall`, jalankan `.venv\Scripts\python.exe -m distrorelay` langsung di jendela PowerShell (bukan lewat pipeline):

| # | Lewat TUI | Yang dicek | Lolos |
|---|---|---|---|
| 2 + 3 | `u` → vm → centang `efi:fedora`, `efi:arch`, `grubdir:grub` (spasi) → preflight + centang USB darurat → backup ke E: → `HAPUS` | Post-check hijau; restart langsung masuk Windows; dashboard sesudahnya tidak lagi menampilkan Fedora/Arch | [x] 2026-10-22: dituntaskan setelah 7b/7q/7c (`LANJUT`, rencana menyusut 8 → 6 → 5 → 3 langkah), post-check 8/8, journal: 3 sesi "dituntaskan oleh" sesi hijau; reset paksa → langsung desktop Windows |
| 7b | Ctrl+C saat langkah 2–3 sedang jalan | Langkah yang sedang jalan selesai lalu berhenti; journal "dihentikan"; `u` berikutnya minta `LANJUT` | [x] 2026-10-22: Ctrl+C saat langkah 2 → "setelah langkah 2 dari 8"; banner kuning + layar `LANJUT` muncul |
| 7c | Tutup jendela (X / Alt+F4) saat eksekusi | Journal "dihentikan" (atau TERPUTUS kalau lewat ±5 detik); Windows tetap boot | [x] 2026-10-22: Alt+F4 saat langkah 2 → `session-end` "Jendela ditutup… setelah langkah 2 dari 5" < 1 detik sesudahnya |
| 7q | Ctrl+Q saat eksekusi | Aplikasi tidak keluar; berhenti dengan rapi seperti 7b | [x] 2026-10-22: "Dihentikan pengguna (Ctrl+Q) setelah langkah 1 dari 6", aplikasi tetap terbuka |
| 7 | `DISTRORELAY_DEV=1`, `DISTRORELAY_FAIL_AFTER=3`, lalu wizard sampai `HAPUS` | TUI menampilkan error, aplikasi tetap hidup, journal TERPUTUS, banner kuning di dashboard | [ ] (opsional; jalur crash sama dengan fase A, tampilannya diuji Pilot) |
| 9 | `p` → pilih backup → centang `EFI\fedora` + entri Fedora → `PULIHKAN` | Folder + entri kembali, tidak dobel setelah 2× reboot | [~] 2026-10-22: backup 20261022-125219 → `EFI\fedora` (6 file, hash cocok) + 1 entri Fedora, 4/4 ok, dashboard menampilkan 1 entri. Cek 2× reboot terputus karena VM freeze (NEM); lanjutkan dari snapshot `fase-b-sesudah-restore` |

Catatan gate fase B: host masih menjalankan Hyper-V (Memory Integrity / HVCI aktif), jadi VirtualBox jalan di mode NEM
dan sesekali freeze total (juga sebelum DistroRelay dijalankan). Script bantu: `tesvm\vmkeys.ps1` (keyboard + screenshot),
`tesvm\reboot-check.ps1` (reset + `bcdedit /enum firmware`). Temuan: dashboard 1 baris lebih tinggi dari layar → baris
status tergulung keluar di terminal sungguhan (diperbaiki dengan CSS, ada test regresi).

## Gate M3: hapus & pulihkan partisi (lewat TUI)

Mulai dari snapshot `siap-uninstall` (sisa palsu dari `fake_leftovers.py` sudah termasuk partisi XBOOTLDR 2 GB; di VM
biasanya `disk0-p6`), lalu pasang kode terbaru dengan `Z:\setup.ps1`. Memilih instalasi Fedora di wizard sekarang ikut
mencentang partisinya. Partisi itu berperan `/boot`, jadi cukup ketik ID partisinya, tanpa pernyataan backup data.

| # | Lewat TUI | Yang dicek | Lolos |
|---|---|---|---|
| M3-1 | `u` → vm → centang `efi:fedora`, `efi:arch`, `grubdir:grub` → preflight → backup → ketik ID partisi + `HAPUS` | Langkah terakhir "Hapus partisi"; post-check hijau termasuk "Partisi yang dihapus sudah tidak ada" dan "Layout disk lain tidak berubah"; Disk Management menampilkan 2 GB unallocated; restart langsung masuk Windows | [ ] |
| M3-2 | Di layar konfirmasi, ketik ID partisi yang salah (mis. `disk0-p4`) | Ditolak "Belum cocok", tidak ada yang diubah | [ ] |
| M3-3 | `p` → backup yang dibuat sebelum M3-1 → centang partisinya → `PULIHKAN` | Partisi kembali dengan offset/ukuran/tipe sama, "UUID filesystem sama"; dashboard menampilkannya lagi sebagai `/boot` Fedora | [ ] |
| M3-4 | Ulangi M3-1, tapi setelah gerbang lolos buat partisi kecil baru di ruang kosong lewat Disk Management, baru ketik `HAPUS` | Langkah partisi berhenti "Layout disk … tidak sama"; partisi Linux masih ada | [ ] |
| M3-5 | Beri partisi Linux drive letter (Disk Management → Change Drive Letter), lalu scan ulang | Partisi tidak bisa dicentang: "punya drive letter / mount point" | [ ] |
| M3-6 (opsional) | Layout dengan partisi Linux tepat sesudah C: (mis. VM tanpa Recovery di antaranya) → centang "Perluas C:" | C: bertambah, post-check "C: diperluas"; sesudahnya Pulihkan menolak partisi itu "ruangnya sudah dipakai" | [ ] |

Di laptop asli, ruang bekas `disk0-p5` **tidak** bersebelahan dengan C: (ada `disk0-p4` Windows Recovery di antaranya),
jadi opsi Perluas C: tidak muncul dan ruangnya dibiarkan unallocated untuk distro berikutnya.

## Gate M4–M5: Linux kit + backup

**Tanpa VM (WSL, mis. Kali).** Hanya `setup --dry-run`, jadi tidak ada paket yang dipasang:

| # | Langkah | Yang dicek | Lolos |
|---|---|---|---|
| K-1 | Windows: `distrorelay profile init` → `profile show` → `kit export --ntfs` | Kit di `C:\DistroRelay\kit`, `kit verify C:\DistroRelay\kit` ok | [ ] |
| K-2 | WSL: `cd /mnt/c/DistroRelay/kit && sh bootstrap.sh info` | Distro terdeteksi (Kali → keluarga debian), "semua hash cocok" | [ ] |
| K-3 | WSL: `sh bootstrap.sh backup` → checklist → ketik beberapa nomor | Paket sistem (linux-image, grub, systemd, …) tidak muncul di checklist; backup di `C:\DistroRelay\linux-backups\<id>`. Karena kit di NTFS (bukan USB), file sensitif ditolak dengan catatan | [ ] |
| K-4 | WSL: `sh bootstrap.sh backups` lalu `sh bootstrap.sh setup --dry-run` | Backup `[ok]`; rencana memuat "Pulihkan dotfiles dari …" | [ ] |
| K-5 | Windows: `distrorelay profile import C:\DistroRelay\linux-backups\<id>` → `ya` | Perubahan tampil, `profile.toml.bak-*` dibuat, `profile show` memuat paket baru + `exclude` | [ ] |
| K-6 | Ubah 1 byte `dotfiles.tar.gz` di backup itu, ulangi K-4 dan K-5 | `backups` menandai RUSAK, `setup` tidak memakainya, `profile import` menolak | [ ] |

**Di Linux sungguhan (VM dual-boot atau install sungguhan, belakangan):**

| # | Langkah | Yang dicek | Lolos |
|---|---|---|---|
| L-1 | Fedora: `sh bootstrap.sh setup` sungguhan | Repo, paket, dan Flatpak terpasang; laporan akhir masuk akal | [ ] |
| L-2 | Arch: `setup` dengan paket AUR, setujui bootstrap `paru` | `paru` dan paket AUR terpasang | [ ] |
| L-3 | Ubuntu 22.04 atau lebih baru: `setup` | apt + Flatpak jalan di Python 3.10 | [ ] |
| L-4 | Distro lama, kit di USB, `~/.ssh` di `dotfiles.sensitive`: `sh bootstrap.sh backup` | `sensitive.tar.gz.gpg` ada di USB; tidak ada salinan tanpa enkripsi | [ ] |
| L-5 | Distro lain: `sh bootstrap.sh setup --backup latest` | `~/.ssh` kembali dengan permission 700/600 (`.pub` 644) | [ ] |
| L-6 | Windows: wizard uninstall untuk partisi root yang punya backup L-4 di drive eksternal | Tidak minta "DATA SUDAH DIBACKUP", peringatan tetap muncul | [ ] |

## Gate M6: ISO manager + shrink C:

**Tanpa VM (di laptop, tidak mengubah sistem).** Butuh internet; ISO Arch ±1,5 GB adalah unduhan terkecil:

| # | Langkah | Yang dicek | Lolos |
|---|---|---|---|
| I-1 | `distrorelay iso keys` | 4 distro + fingerprint tampil, `gpgv:` menunjuk ke file yang ada | [ ] |
| I-2 | `distrorelay iso check` (USB Ventoy dicabut) | Versi terbaru 4 distro tampil; catatan "USB Ventoy tidak terdeteksi" | [ ] |
| I-3 | `distrorelay iso download arch`, Ctrl+C di tengah, jalankan lagi | Unduhan lanjut dari posisi terakhir (tidak mulai dari 0); akhirnya tanda tangan "sah, oleh Pierre Schmitz" + SHA256 `[ok]` | [ ] |
| I-4 | Ubah 1 byte ISO di `%LOCALAPPDATA%\DistroRelay\iso\arch\...` (mis. pakai HxD), lalu `iso verify arch` | `[GAGAL]`; ISO disisihkan jadi `.rejected` | [ ] |
| I-5 | `iso download arch` lagi, lalu colok USB Ventoy → `iso check` | Unduh ulang dari awal + lolos; `check` menampilkan isi USB dan status Arch | [ ] |
| I-6 | `distrorelay iso copy arch` | Tanda tangan dicek ulang, salin + "Baca ulang dari USB" `[ok]`; ISO Arch lama (kalau ada) tetap di USB; `iso check` → "sudah terbaru" | [ ] |
| I-7 | Boot laptop/VM dari USB Ventoy itu, pilih ISO Arch | Menu Ventoy menampilkan ISO, dan ISO bisa boot | [ ] |
| I-8 | (opsional, ±2,7 GB) `iso download fedora` → `iso copy fedora` | Sama seperti I-3/I-6 dengan CHECKSUM clearsigned Fedora | [ ] |
| I-9 | `distrorelay shrink-c` (tanpa `--size`) | Ukuran C:, ruang kosong, dan "maksimal yang bisa dibebaskan" masuk akal dibanding Disk Management. Tidak ada yang berubah | [ ] |

**Di VM (snapshot dulu), karena mengubah partisi:**

| # | Langkah | Yang dicek | Lolos |
|---|---|---|---|
| S-1 | `distrorelay shrink-c --size 10` → ketik `kecilkan` (huruf kecil) | Dibatalkan, tidak ada yang berubah | [ ] |
| S-2 | `shrink-c --size 10` → ketik `KECILKAN` | C: berkurang 10 GB, ruang kosong 10 GB muncul tepat sesudah C: di Disk Management; `journal` menampilkan sesi selesai + post-check ok | [ ] |
| S-3 | `shrink-c --size <lebih dari maksimal>` | `[GAGAL]` dengan alasan (batas Windows / sisa kosong 20 GB), tidak ada yang berubah | [ ] |
| S-4 | Reboot VM | Windows boot normal, `chkdsk C:` (read-only) bersih | [ ] |

## Gate M7: distrorelay.exe

**Tanpa VM** (kecuali E-6). Build sekali di folder repo: `.venv\Scripts\python.exe -m pip install -e ".[dev,build]"`
lalu `.venv\Scripts\python.exe -m PyInstaller packaging\distrorelay.spec --noconfirm`:

| # | Langkah | Yang dicek | Lolos |
|---|---|---|---|
| E-1 | Salin `dist\distrorelay.exe` ke folder lain (mis. Downloads), PowerShell biasa (bukan admin): `.\distrorelay.exe --version` lalu `.\distrorelay.exe selftest` | `DistroRelay 0.7.0`; semua `[ok]`, termasuk "exe (PyInstaller)", "Linux kit … lolos verifikasi", dan "TUI … tampil" | [ ] |
| E-2 | Klik dua kali `distrorelay.exe` di Explorer | Dialog UAC → jendela baru berisi dashboard TUI + hasil scan; `q` keluar | [ ] |
| E-3 | PowerShell biasa: `.\distrorelay.exe scan` | UAC → jendela admin baru menampilkan hasil scan + "Tekan Enter untuk menutup" | [ ] |
| E-4 | `.\distrorelay.exe iso check`, `.\distrorelay.exe profile show`, `.\distrorelay.exe journal` | Jalan langsung di jendela yang sama, tanpa UAC | [ ] |
| E-5 | `.\distrorelay.exe kit export --ntfs`, lalu di WSL `cd /mnt/c/DistroRelay/kit && sh bootstrap.sh info` | Kit dari `.exe` lolos cek hash di Linux (sama seperti K-2) | [ ] |
| E-6 | Di VM/PC **tanpa Python**: salin `.exe`, jalankan `selftest` dan `scan` | Sama seperti E-1/E-3, tanpa error DLL/modul | [ ] |
| E-7 | GitHub: `git tag v0.7.0` → `git push origin v0.7.0` | Actions "Rilis exe" hijau; rilis **draft** berisi `distrorelay.exe` + `.sha256`; hash file unduhan sama; `selftest` lolos. Publish manual kalau sudah yakin | [ ] |

## Khusus M2a (sudah bisa dicoba sekarang)

- [ ] `distrorelay backup --usb <huruf>` di VM: semua verifikasi `[ok]`, folder `esp\` di USB lengkap
- [ ] Ubah satu file di salinan USB → verifikasi ulang mendeteksi hash tidak cocok
- [ ] `distrorelay plan --select ...` menampilkan urutan: Windows pertama → fallback → entri → folder
