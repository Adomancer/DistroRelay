#!/bin/sh
# DistroRelay Linux kit. Jalankan dari folder kit (USB atau partisi Windows):
#   sh bootstrap.sh info            # distro + isi kit, tidak mengubah apa pun
#   sh bootstrap.sh setup --dry-run # rencana setup saja
#   sh bootstrap.sh setup           # pasang repo, paket, Flatpak, AUR, dotfiles dari profil
# Memastikan python3 (>= 3.8) ada dulu; di Arch minimal python belum terpasang.
set -eu

KIT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

python_ok() {
    command -v python3 >/dev/null 2>&1 &&
        python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null
}

if ! python_ok; then
    ID="" ID_LIKE=""
    # shellcheck disable=SC1091
    [ -r /etc/os-release ] && . /etc/os-release
    echo "python3 (>= 3.8) belum ada; memasang lewat package manager (butuh sudo)..."
    case " ${ID} ${ID_LIKE} " in
        *" arch "*) sudo pacman -Syu --needed --noconfirm python ;;
        *" fedora "*) sudo dnf install -y python3 ;;
        *" debian "* | *" ubuntu "*) sudo apt-get update && sudo apt-get install -y python3 ;;
        *)
            echo "Distro '${ID}' tidak dikenali. Pasang python3 >= 3.8 sendiri, lalu jalankan lagi." >&2
            exit 1
            ;;
    esac
    python_ok || { echo "python3 >= 3.8 masih belum tersedia." >&2; exit 1; }
fi

PYTHONPATH="${KIT_DIR}${PYTHONPATH:+:${PYTHONPATH}}" exec python3 -m distrorelay_linux --kit "$KIT_DIR" "$@"
