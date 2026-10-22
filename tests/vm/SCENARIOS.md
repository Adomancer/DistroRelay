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

## Khusus M2a (sudah bisa dicoba sekarang)

- [ ] `distrorelay backup --usb <huruf>` di VM: semua verifikasi `[ok]`, folder `esp\` di USB lengkap
- [ ] Ubah satu file di salinan USB → verifikasi ulang mendeteksi hash tidak cocok
- [ ] `distrorelay plan --select ...` menampilkan urutan: Windows pertama → fallback → entri → folder
