"""Input dari terminal. `ask` untuk jawaban biasa (di-strip); `ask_word` untuk kata konfirmasi (HAPUS, LANJUT, PULIHKAN,
TUNTAS, KECILKAN), yang tidak di-strip. Test memalsukan fungsi di modul ini (`prompts.ask`), jadi pemanggil memakainya
lewat nama modul."""

from __future__ import annotations


def ask(prompt: str) -> str:
    # PowerShell 5.1 dengan console UTF-8 menyisipkan BOM di depan teks yang di-pipe (`'ya' | distrorelay …` sampai
    # sebagai '\ufeffya'); BOM bukan spasi, jadi tidak ikut terbuang oleh strip().
    try:
        return input(prompt).lstrip("\ufeff").strip()
    except EOFError:
        return ""


def ask_word(prompt: str) -> str:
    """Untuk kata konfirmasi (HAPUS, LANJUT, PULIHKAN): tidak di-strip, supaya spasi di depan tetap ditolak. BOM juga
    tidak dibuang: kata konfirmasi harus diketik, bukan di-pipe."""
    try:
        return input(prompt)
    except EOFError:
        return ""


def pause() -> None:
    try:
        input("\nTekan Enter untuk menutup jendela ini…")
    except EOFError:
        pass
