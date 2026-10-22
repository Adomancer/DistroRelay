# DistroRelay

Toolkit distro-hopping untuk laptop dual-boot Windows + Linux: **backup → uninstall bersih → install distro baru → setup otomatis**, dengan Windows sebagai pusat kendali.

Masalah awalnya: setelah partisi Linux di-wipe, GRUB masih muncul saat boot karena folder bootloader di EFI System Partition (ESP), entri boot di NVRAM, dan partisi pendukung seperti `/boot` tidak ikut terhapus.

## Status: M0 + M1 + M2 + M3 (CLI + wizard TUI), M4 + M5 (Linux kit + backup), M6 (ISO manager + shrink C:)

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
| M4 | Profil (TOML) + peta nama paket per distro, export Linux kit ke USB/NTFS, `setup` di distro baru | ✅ kode + test simulasi; belum dicoba di Linux sungguhan |
| M5 | `backup` di distro lama (paket dikurasi, dotfiles, file sensitif terenkripsi di USB), `profile import`, aturan backup bertingkat (a) | ✅ kode + test simulasi; belum dicoba di Linux sungguhan |
| M6 | ISO manager (cek versi terbaru, unduh yang bisa dilanjutkan, verifikasi GPG + SHA256, salin ke Ventoy), shrink C: | ✅ kode + test (tanda tangan asli distro dicek dengan gpgv); unduh ISO penuh, salin ke Ventoy, dan shrink di VM belum |
| M7 | Rilis `.exe` (PyInstaller) | belum |

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

## Linux kit (M4–M5): pasang ulang aplikasi di distro baru

Profil distro-hopping (aplikasi, repo, dotfiles) diedit di Windows sebagai TOML, lalu diekspor bersama kode kit ke
flashdisk dan/atau partisi Windows. Di distro baru, kit memasang semuanya lewat package manager distro itu.

```powershell
# Di Windows (tidak butuh admin)
.venv\Scripts\python.exe -m distrorelay profile init     # %LOCALAPPDATA%\DistroRelay\profile.toml, lalu edit
.venv\Scripts\python.exe -m distrorelay profile show     # cek + pratinjau nama paket di Fedora/Arch/Ubuntu
.venv\Scripts\python.exe -m distrorelay kit export --drive E --ntfs   # E:\DistroRelay\kit dan C:\DistroRelay\kit
.venv\Scripts\python.exe -m distrorelay fast-startup     # harus MATI kalau Linux perlu menulis ke partisi Windows
```

```sh
# Di Linux, dari folder kit (mis. /run/media/$USER/USB/DistroRelay/kit atau /mnt/c/DistroRelay/kit di WSL)
sh bootstrap.sh info             # distro + isi kit; hash semua file dicek dulu
sh bootstrap.sh setup --dry-run  # rencana + perintah persisnya, tidak menjalankan apa pun
sh bootstrap.sh setup            # repo -> paket -> Flathub/Flatpak -> AUR (paru, dengan persetujuan) -> laporan
```

- Distro yang didukung: keluarga Fedora (dnf), Arch (pacman + paru untuk AUR), Debian/Ubuntu/Mint (apt), dikenali dari
  `/etc/os-release` (`ID` + `ID_LIKE`, jadi EndeavourOS, Nobara, Mint, Kali ikut).
- Nama paket di profil adalah nama logis; terjemahannya per distro ada di
  [`src/distrorelay/common/data/packages.toml`](src/distrorelay/common/data/packages.toml) dan bisa ditimpa lewat
  `%LOCALAPPDATA%\DistroRelay\packages.toml`.
- Kit tidak butuh `pip install`: Python stdlib saja, jalan di Python 3.8+ (`bootstrap.sh` memasang python3 kalau belum ada).
- `setup` hanya menambah. Kalau pemasangan paket sekaligus gagal, paketnya dicoba satu per satu supaya ketahuan mana
  yang bermasalah. Dotfiles dipulihkan dari backup terbaru (lihat di bawah); file lama di-rename, bukan ditimpa.

### Backup dari distro lama (M5)

Sebelum pindah distro, jalankan `backup` di distro lama dari folder kit. Hasilnya bisa dipakai `setup` di distro baru,
dan dimasukkan ke profil di Windows.

```sh
sh bootstrap.sh backup           # daftar paket (dikurasi) + dotfiles (+ file sensitif terenkripsi gpg)
sh bootstrap.sh backup --yes     # tanpa checklist kurasi
sh bootstrap.sh backups          # daftar backup di kit ini + verifikasi hash
sh bootstrap.sh setup --backup latest|none|DIR   # default: backup terbaru yang lolos verifikasi
```

```powershell
# Di Windows: tambahkan paket hasil backup ke profil (hanya menambah; profil lama disimpan sebagai profile.toml.bak-*)
.venv\Scripts\python.exe -m distrorelay profile import E:\DistroRelay\linux-backups\20261022-150000-fedora
```

- **Ini bukan backup dokumen/foto/kode.** Isinya hanya daftar paket dan dotfiles. Data pribadi tetap perlu rsync,
  restic, atau Déjà Dup.
- Paket bawaan distro (kernel, firmware, bootloader, dsb.) disaring otomatis lewat pola di
  [`base_packages/`](src/distrorelay/common/data/base_packages). Sisanya bisa dikurasi di checklist; yang tidak dibawa
  dicatat di `apps.exclude`, jadi tidak ditanya lagi lain kali.
- `dotfiles.sensitive` (mis. `~/.ssh`) **selalu** dienkripsi gpg (AES256, passphrase) dan hanya disimpan di USB. Kalau
  kit ini di partisi Windows, file sensitif ditaruh di kit lain yang ada di USB, atau tidak dibackup sama sekali.
  Isinya tidak pernah ditulis ke disk tanpa enkripsi, dan dipulihkan dengan permission 700/600.
- Backup mencatat UUID filesystem `/` dan `/home` (+ LUKS). Saat uninstall di Windows, partisi yang UUID-nya tercatat
  di backup Linux terverifikasi di drive eksternal tidak perlu pernyataan "DATA SUDAH DIBACKUP" (tetap ada peringatan).

## ISO manager (M6): siapkan USB Ventoy

Cek versi terbaru Fedora, Arch, Ubuntu LTS, dan Linux Mint dari feed resmi, unduh ISO-nya, verifikasi, lalu salin ke
USB [Ventoy](https://www.ventoy.net). Tidak butuh admin.

```powershell
.venv\Scripts\python.exe -m distrorelay iso check               # versi terbaru vs ISO di USB Ventoy (label "Ventoy")
.venv\Scripts\python.exe -m distrorelay iso download fedora     # unduh + verifikasi; Ctrl+C lalu ulangi = dilanjutkan
.venv\Scripts\python.exe -m distrorelay iso download mint --edition xfce
.venv\Scripts\python.exe -m distrorelay iso verify fedora       # verifikasi ulang tanpa internet
.venv\Scripts\python.exe -m distrorelay iso copy fedora         # verifikasi ulang, lalu salin + baca ulang dari USB
.venv\Scripts\python.exe -m distrorelay iso keys                # kunci bawaan + lokasi gpgv
```

- **Dua lapis verifikasi, dua-duanya wajib lolos** sebelum ISO boleh disalin:
  1. Tanda tangan GPG dicek `gpgv` terhadap keyring **bawaan aplikasi**
     ([`windows/iso/keys/`](src/distrorelay/windows/iso/keys)); kunci utama penanda tangan harus ada di daftar
     fingerprint yang di-pin di kode sumber tiap distro. Fedora: `CHECKSUM` (clearsigned), Ubuntu: `SHA256SUMS.gpg`,
     Mint: `sha256sum.txt.gpg`, Arch: ISO-nya langsung (`.iso.sig`).
  2. SHA256 ISO harus sama dengan nilai di file checksum yang sudah terverifikasi (Arch: nilai dari feed resmi).
- Butuh `gpgv`: dari GnuPG (`winget install GnuPG.GnuPG`) atau yang ikut Git for Windows; dicari otomatis.
- Unduhan di `%LOCALAPPDATA%\DistroRelay\iso\<distro>\<versi>-<edisi>\`. ISO yang gagal SHA256/tanda tangan
  disisihkan sebagai `.rejected`.
- Salin ke USB: SHA256 dihitung saat menyalin, lalu salinannya dibaca ulang. File yang sudah ada dengan isi berbeda tidak
  ditimpa, USB FAT32 ditolak untuk ISO > 4 GB, dan **ISO versi lama tidak dihapus otomatis**.
- Kunci Fedora berganti tiap rilis. Rilis yang kuncinya belum ada di keyring bawaan ditolak sampai DistroRelay diperbarui.

### Shrink C: (MENGUBAH partisi, coba di VM dulu)

```powershell
.venv\Scripts\python.exe -m distrorelay shrink-c              # info: ukuran, ruang kosong, maksimal yang bisa dibebaskan
.venv\Scripts\python.exe -m distrorelay shrink-c --size 60    # bebaskan 60 GB, konfirmasi ketik KECILKAN
```

Sama seperti *Shrink Volume* di Disk Management (`Resize-Partition`). Ditolak kalau melewati batas Windows
(`Get-PartitionSupportedSize`) atau kalau sisa ruang kosong C: jadi kurang dari 20 GB. Script mencocokkan ulang
GUID/offset/ukuran C: tepat sebelum mengecilkan, dicatat di journal, lalu dicek ulang. Ruang kosongnya muncul tepat
sesudah C: dan bisa langsung dipakai installer Linux.

## Struktur

```
src/distrorelay_linux/    Linux kit (stdlib, Python 3.8+): cli, distro, pm, provision, dotfiles, backup, kit, bootstrap.sh
src/distrorelay/common/   profil + peta paket, dipakai Windows dan Linux kit
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
   ├─ workflow.py         UninstallFlow & RestoreFlow: satu-satunya jalan ke perubahan sistem
   ├─ kit_export.py       export Linux kit + profil, cek Fast Startup
   ├─ shrink.py           shrink C: (rencana, script persis, journal, post-check)
   └─ iso/                ISO manager: sources/ per distro, keys/ (keyring bawaan), download, verify, catalog, manager
```
