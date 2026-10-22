# Prosedur darurat DistroRelay

Pakai dokumen ini kalau setelah uninstall laptop **tidak bisa boot ke Windows**, atau ada yang terhapus keliru.
Latih dulu prosedur ini di VM (skenario 8 di `tests/vm/SCENARIOS.md`) sebelum menjalankan uninstall di laptop asli.

## Yang perlu disiapkan sebelumnya

- **USB darurat Windows**: USB installer biasa, atau Ventoy berisi ISO Windows. Sudah dicoba boot sampai Command Prompt.
- **Recovery key BitLocker** dari akun Microsoft, kalau Device Encryption aktif.
- **Backup DistroRelay** di drive eksternal (flashdisk atau HDD eksternal): `<drive>:\DistroRelay\backups\<id>\`.
  Drive ini **berbeda** dari USB darurat: HDD berisi backup tidak bisa dipakai untuk boot.

Isi folder backup:

| File | Isi | Dipakai di bagian |
|---|---|---|
| `esp.zip` | Seluruh isi ESP | A |
| `esp\` | Isi ESP yang sama, berupa folder biasa (bisa di-`xcopy` dari WinRE) | A |
| `bcd-store.bak` | BCD store Windows (`bcdedit /export`) | B |
| `firmware-entries.txt` / `.json` | Daftar entri boot firmware (nama, GUID, path `.efi`) | C |
| `nvram-raw.json` | Isi mentah `BootOrder` dan `Boot####` | C (manual) |
| `partitions.json` | Metadata semua partisi (offset, ukuran, tipe GPT, GUID) | D |
| `fsinfo.json` | Jenis + UUID filesystem tiap partisi (dari superblock), untuk memastikan partisi yang dipulihkan isinya sama | D |
| `manifest.json` | Hash SHA256 semua file di atas | semua |

## Masuk ke Command Prompt darurat

1. Colok USB darurat, nyalakan laptop, lalu pilih USB dari menu boot (laptop HP: tekan **F9** saat logo muncul).
2. Pilih bahasa → **Next** → **Repair your computer** → **Troubleshoot** → **Command Prompt**.
3. Kalau BitLocker aktif, drive Windows terkunci. Buka dulu:
   ```
   manage-bde -unlock X: -RecoveryPassword <48 digit recovery key>
   ```
4. Cari huruf drive. Di WinRE, drive Windows **sering bukan C:**:
   ```
   diskpart
   list vol
   ```
   - Volume Windows: NTFS besar (±474 GB, label "Windows"). Catat hurufnya, sebut saja **X**.
   - ESP: FAT32 ±260 MB (label "SYSTEM").
   ```
   sel vol <nomor ESP>
   assign letter=S
   exit
   ```

## A. File boot Windows rusak / Windows tidak bisa boot

1. Coba dulu **tanpa `/s`**:
   ```
   bcdboot X:\Windows /f UEFI
   ```
   Perintah ini menyalin file boot ke ESP **dan** membuat entri Windows Boot Manager di NVRAM.
2. Kalau langkah 1 gagal:
   ```
   bcdboot X:\Windows /s S: /f UEFI
   ```
   File boot tersalin, tapi **entri NVRAM tidak dibuat**. Laptop akan boot lewat file fallback `\EFI\Boot\bootx64.efi`.
   Setelah Windows menyala, buka Command Prompt (Administrator) dan jalankan `bcdboot C:\Windows` supaya entrinya dibuat.
3. Alternatif: kembalikan isi ESP dari backup (`U:` = huruf drive backup di WinRE; colok HDD-nya dulu, lalu cek lewat `list vol`):
   ```
   xcopy U:\DistroRelay\backups\<id>\esp S:\ /e /h /k /y
   ```

## B. BCD store rusak

```
bcdedit /import U:\DistroRelay\backups\<id>\bcd-store.bak
```
Atau bangun ulang dengan bagian A langkah 1.

## C. Entri firmware hilang / urutan boot salah

- **Entri Windows hilang atau bukan urutan pertama** (dari Windows atau WinRE):
  ```
  bcdedit /set {fwbootmgr} displayorder {bootmgr} /addfirst
  ```
  Kalau entrinya hilang sama sekali: `bcdboot C:\Windows` (bagian A).
- **Entri Linux terhapus keliru**: pakai menu Pulihkan di DistroRelay (M2b). Alternatifnya, tambah lewat menu firmware
  ("Add boot option", arahkan ke file `.efi` dari `firmware-entries.txt`), atau dari Linux live USB:
  ```
  efibootmgr -c -d /dev/nvme0n1 -p 1 -L Fedora -l '\EFI\fedora\shimx64.efi'
  ```
- Nomor `Boot####` dan optional data entri yang dibuat ulang tidak akan sama persis dengan aslinya. Data mentahnya ada di `nvram-raw.json` (hanya untuk pemulihan manual tingkat lanjut).

## D. Partisi terhapus keliru (M3)

`Remove-Partition` hanya menghapus entri di tabel GPT; datanya belum ditimpa. Selama ruang bekasnya **belum dipakai**
(belum diisi distro baru, dan C: belum diperluas ke sana), partisi bisa dibuat ulang dengan offset, ukuran, dan tipe
yang sama persis, dan isinya kembali utuh.

Cara termudah (Windows masih bisa boot): menu **Pulihkan** (`p` di dashboard), centang partisinya, ketik `PULIHKAN`.
Lewat CLI:
```powershell
.venv\Scripts\python.exe -m distrorelay restore --backup <id> --partition disk0-p5
```
DistroRelay menolak kalau ruangnya sudah dipakai partisi lain, lalu sesudahnya scan ulang dan mencocokkan UUID
filesystem dengan `fsinfo.json` di backup.

Manual (PowerShell Administrator), dengan angka dari `partitions.json` (cari partisi dengan `PartitionNumber` yang dihapus):
```powershell
Get-Partition -DiskNumber 0 | Format-Table PartitionNumber, Offset, Size   # pastikan ruangnya kosong
New-Partition -DiskNumber 0 -Offset <Offset> -Size <Size> -GptType '{<GptType>}'
```
PARTUUID-nya akan berbeda, tapi UUID filesystem dan isinya tetap sama. Kalau distro yang memakainya merujuk partisi
lewat PARTUUID (jarang; biasanya lewat UUID filesystem), perbarui rujukannya dari live USB Linux.
