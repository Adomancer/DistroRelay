"""ISO manager (M6): sumber versi per distro, verifikasi GPG + SHA256, unduhan yang bisa dilanjutkan, salin ke Ventoy.

Test tanda tangan memakai gpgv sungguhan (dilewati kalau tidak ada) dengan:
- file asli dari Fedora/Ubuntu/Mint + keyring bawaan aplikasi (membuktikan kunci yang di-pin benar), dan
- kunci test + kunci "penyerang" (tests/fixtures/iso/test-keyring.gpg) untuk ISO/checksum yang dimodifikasi.
"""

from __future__ import annotations

import hashlib
import http.server
import json
import threading
from argparse import Namespace
from pathlib import Path

import pytest

from distrorelay.windows import iso as iso_mod
from distrorelay.windows.iso import (
    CLEARSIGNED_CHECKSUM,
    DETACHED_CHECKSUM,
    DETACHED_ISO,
    IsoError,
    KeySpec,
    Release,
    catalog,
    download,
    manager,
    sources,
    verify,
)
from distrorelay.windows.iso.sources import arch, fedora, mint, ubuntu
from distrorelay.windows.runner import CommandResult

FIXTURES = Path(__file__).parent / "fixtures" / "iso"
GPGV = verify.find_gpgv()
needs_gpgv = pytest.mark.skipif(GPGV is None, reason="gpgv tidak ditemukan")

FAKE_ISO = b"DISTRORELAY-FAKE-ISO\n" * 4096
FAKE_SHA = hashlib.sha256(FAKE_ISO).hexdigest()
TEST_FPR = "D64935B716063720C60B3BF0E95FA168EC1351D5"
EVIL_FPR = "4987D77B8E456482D041FCDE782468B62A05C21A"
TEST_KEYS = KeySpec(FIXTURES / "test-keyring.gpg", frozenset({TEST_FPR}))
BASE = "https://example.invalid/fake/"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def fake_fetch(mapping: dict[str, bytes]):
    def fetch(url: str) -> bytes:
        if url not in mapping:
            raise download.DownloadError(f"tidak ada di fixture: {url}")
        return mapping[url]

    return fetch


# --- Sumber versi per distro -------------------------------------------------------------------

FEEDS = {
    fedora.FEED: fixture("fedora-releases.json"),
    "https://download.fedoraproject.org/pub/fedora/linux/releases/44/Workstation/x86_64/iso/": fixture("fedora-listing.html"),
    arch.FEED: fixture("arch-releases.json"),
    ubuntu.FEED: fixture("meta-release-lts"),
    "https://releases.ubuntu.com/resolute/SHA256SUMS": fixture("SHA256SUMS"),
    mint.BASE: fixture("mint-stable.html"),
    mint.BASE + "22.3/sha256sum.txt": fixture("sha256sum.txt"),
}


def test_fedora_picks_newest_stable_workstation_not_beta_or_other_arch():
    release = fedora.latest(fake_fetch(FEEDS))
    assert (release.version, release.edition) == ("44", "Workstation")
    assert release.iso_name == "Fedora-Workstation-Live-44-1.7.x86_64.iso"
    assert release.checksum_url.endswith("/44/Workstation/x86_64/iso/Fedora-Workstation-44-1.7-x86_64-CHECKSUM")
    assert release.scheme == CLEARSIGNED_CHECKSUM and release.size == 2851612672
    assert release.problems() == []


def test_fedora_other_edition_and_unknown_edition():
    feed = json.loads(fixture("fedora-releases.json"))
    assert fedora.pick(feed, "kde")["link"].endswith("Fedora-KDE-Desktop-Live-44-1.7.x86_64.iso")
    assert fedora.pick(feed, "Server")["link"].endswith("Fedora-Server-dvd-x86_64-44-1.7.iso")  # tanpa Live
    with pytest.raises(IsoError, match="tidak ada di feed"):
        fedora.pick(feed, "Gentoo")


def test_arch_release_from_feed():
    release = arch.latest(fake_fetch(FEEDS))
    assert release.version == "2026.10.01"
    assert release.iso_url == "https://geo.mirror.pkgbuild.com/iso/2026.10.01/archlinux-2026.10.01-x86_64.iso"
    assert release.signature_url == release.iso_url + ".sig"
    assert release.scheme == DETACHED_ISO and len(release.sha256) == 64 and release.size == 1640497152


def test_ubuntu_version_comes_from_checksum_file_names():
    release = ubuntu.latest(fake_fetch(FEEDS))
    assert (release.version, release.iso_name) == ("26.04.1", "ubuntu-26.04.1-desktop-amd64.iso")
    assert release.signature_url == "https://releases.ubuntu.com/resolute/SHA256SUMS.gpg"


def test_mint_newest_folder_and_editions():
    assert mint.latest(fake_fetch(FEEDS)).iso_name == "linuxmint-22.3-cinnamon-64bit.iso"
    assert mint.latest(fake_fetch(FEEDS), "xfce").iso_name == "linuxmint-22.3-xfce-64bit.iso"
    with pytest.raises(IsoError, match="tidak dikenal"):
        mint.latest(fake_fetch(FEEDS), "kde")
    no_iso = dict(FEEDS, **{mint.BASE + "22.3/sha256sum.txt": b""})
    with pytest.raises(IsoError, match="tidak tercantum"):
        mint.latest(fake_fetch(no_iso))


def test_changed_feed_format_marks_only_that_distro_unknown(tmp_path):
    broken = dict(FEEDS, **{fedora.FEED: b"<html>maintenance</html>", arch.FEED: b'{"releases": []}'})
    rows = catalog.check(sources.SOURCES, fake_fetch(broken), downloads_root=tmp_path)
    by_distro = {row.distro: row for row in rows}
    assert by_distro["fedora"].status == catalog.UNKNOWN and "berubah" in by_distro["fedora"].error
    assert by_distro["arch"].status == catalog.UNKNOWN
    assert by_distro["ubuntu"].latest.version == "26.04.1" and by_distro["mint"].latest.version == "22.3"


def test_release_rejects_unsafe_names_and_plain_http():
    good = Release("x", "1", "e", "x.iso", "https://a/x.iso", DETACHED_CHECKSUM, "https://a/SUMS", "https://a/SUMS.gpg")
    assert good.problems() == []
    assert Release("x", "1", "e", "../x.iso", "https://a/x.iso", DETACHED_CHECKSUM, "https://a/S", "https://a/S.gpg").problems()
    assert any("HTTPS" in p for p in Release("x", "1", "e", "x.iso", "http://a/x.iso", DETACHED_CHECKSUM,
                                                 "https://a/S", "https://a/S.gpg").problems())
    assert any("SHA256" in p for p in Release("x", "1", "e", "x.iso", "https://a/x.iso", DETACHED_ISO,
                                                  signature_url="https://a/x.iso.sig").problems())
    with pytest.raises(IsoError):
        Release.from_dict({**good.to_dict(), "iso_name": "..\\x.iso"})


def test_parser_choices_match_sources():
    from distrorelay.__main__ import ISO_DISTROS

    assert set(ISO_DISTROS) == set(sources.SOURCES)


# --- Keyring bawaan ----------------------------------------------------------------------------


def _primary_fingerprints(data: bytes) -> set[str]:
    """Fingerprint v4 kunci utama (paket tag 6) di keyring biner, tanpa gpg."""
    found, i = set(), 0
    while i < len(data):
        header = data[i]
        i += 1
        if header & 0x40:
            tag, first = header & 0x3F, data[i]
            i += 1
            if first < 192:
                length = first
            elif first < 224:
                length = ((first - 192) << 8) + data[i] + 192
                i += 1
            else:
                assert first == 255, "panjang parsial tidak dipakai di keyring"
                length = int.from_bytes(data[i:i + 4], "big")
                i += 4
        else:
            tag, size = (header >> 2) & 0x0F, {0: 1, 1: 2, 2: 4}[header & 3]
            length = int.from_bytes(data[i:i + size], "big")
            i += size
        body = data[i:i + length]
        i += length
        if tag == 6:
            assert body[0] == 4
            found.add(hashlib.sha1(b"\x99" + len(body).to_bytes(2, "big") + body).hexdigest().upper())
    return found


@pytest.mark.parametrize("distro", sorted(sources.SOURCES))
def test_bundled_keyring_holds_exactly_the_pinned_keys(distro):
    keys = sources.get(distro).KEYS
    assert _primary_fingerprints(keys.path.read_bytes()) == set(keys.fingerprints)


# --- Tanda tangan asli dari distro --------------------------------------------------------------


@needs_gpgv
def test_real_fedora_checksum_verifies_and_returns_only_signed_text():
    check = verify.check_signature(fedora.KEYS, FIXTURES / "Fedora-Workstation-44-1.7-x86_64-CHECKSUM", gpgv=GPGV)
    assert check.ok, check.message
    assert check.signer == "36F612DCF27F7D1A48A835E4DBFCF71C6D9F90A6"
    sums = verify.parse_checksums(check.signed_text.decode())
    assert sums["Fedora-Workstation-Live-44-1.7.x86_64.iso"].startswith("1620295f")
    assert b"BEGIN PGP" not in check.signed_text


@needs_gpgv
@pytest.mark.parametrize("source, data", [(ubuntu, "SHA256SUMS"), (mint, "sha256sum.txt")])
def test_real_detached_signatures_verify(source, data):
    check = verify.check_signature(source.KEYS, FIXTURES / data, FIXTURES / f"{data}.gpg", gpgv=GPGV)
    assert check.ok, check.message
    assert check.signer in source.KEYS.fingerprints


@needs_gpgv
def test_real_checksum_with_one_changed_byte_is_rejected(tmp_path):
    tampered = bytearray(fixture("SHA256SUMS"))
    tampered[0] = ord("0") if tampered[0] != ord("0") else ord("1")
    (tmp_path / "SHA256SUMS").write_bytes(tampered)
    check = verify.check_signature(ubuntu.KEYS, tmp_path / "SHA256SUMS", FIXTURES / "SHA256SUMS.gpg", gpgv=GPGV)
    assert not check.ok and "TIDAK sah" in check.message and check.judged


@needs_gpgv
def test_signature_from_another_distros_key_is_rejected():
    check = verify.check_signature(mint.KEYS, FIXTURES / "SHA256SUMS", FIXTURES / "SHA256SUMS.gpg", gpgv=GPGV)
    assert not check.ok and "bukan kunci resmi" in check.message


# --- Penilaian status gpgv (tanpa gpgv) ----------------------------------------------------------

VALID = "[GNUPG:] VALIDSIG {fpr} 2026-04-24 1777044959 0 4 0 1 {hash} 01 {fpr}"


def fake_gpgv(stdout: str, returncode: int = 0):
    def run(args, cwd):
        return CommandResult(tuple(args), returncode, stdout)

    return run


@pytest.mark.parametrize(
    "stdout, returncode, expected",
    [
        (f"[GNUPG:] GOODSIG X Test\n{VALID.format(fpr=TEST_FPR, hash=10)}", 0, "sah"),
        (f"[GNUPG:] GOODSIG X Test\n{VALID.format(fpr=EVIL_FPR, hash=10)}", 0, "tidak di-pin"),
        (f"[GNUPG:] GOODSIG X Test\n{VALID.format(fpr=TEST_FPR, hash=2)}", 0, "hash lemah"),
        (f"[GNUPG:] EXPKEYSIG X Test\n{VALID.format(fpr=TEST_FPR, hash=10)}", 0, "TIDAK sah"),
        (f"{VALID.format(fpr=TEST_FPR, hash=10)}\n[GNUPG:] BADSIG X Test", 1, "TIDAK sah"),
        ("", 2, "gpgv gagal"),
        (f"[GNUPG:] GOODSIG X Test\n{VALID.format(fpr=TEST_FPR, hash=10)}", 2, "gpgv gagal"),
    ],
)
def test_gpgv_status_judgement(tmp_path, stdout, returncode, expected):
    data = tmp_path / "data"
    data.write_bytes(b"x")
    check = verify.check_signature(TEST_KEYS, data, data, gpgv="gpgv", run=fake_gpgv(stdout, returncode))
    assert check.ok == (expected == "sah")
    assert expected in check.message


def test_keyring_is_passed_relative_from_a_temp_working_dir(tmp_path):
    seen = {}

    def run(args, cwd):
        seen["args"], seen["cwd"] = list(args), Path(cwd)
        seen["copied"] = (Path(cwd) / "keyring.gpg").read_bytes()
        return CommandResult(tuple(args), 2, "")

    data = tmp_path / "SUMS"
    data.write_bytes(b"x")
    verify.check_signature(TEST_KEYS, data, data, gpgv="gpgv", run=run)
    assert seen["args"][1:5] == ["--homedir", ".", "--keyring", "./keyring.gpg"]
    assert seen["copied"] == TEST_KEYS.path.read_bytes()
    assert not seen["cwd"].exists()  # folder sementara dibersihkan


def test_parse_checksums_formats_and_conflicts():
    text = (
        "abc *not-a-hash.iso\n"
        f"{'a' * 64} *gnu.iso\n"
        f"{'B' * 64}  text-mode.iso\n"
        f"SHA256 (bsd.iso) = {'c' * 64}\n"
        f"{'d' * 64} *dup.iso\n{'e' * 64} *dup.iso\n"
    )
    assert verify.parse_checksums(text) == {"gnu.iso": "a" * 64, "text-mode.iso": "b" * 64, "bsd.iso": "c" * 64}


# --- Alur unduh + verifikasi (kunci test) ----------------------------------------------------------


def fake_release(scheme: str) -> Release:
    iso_url = BASE + "fake-1.0-x86_64.iso"
    if scheme == DETACHED_CHECKSUM:
        return Release("fake", "1.0", "x86_64", "fake-1.0-x86_64.iso", iso_url, scheme,
                       BASE + "fake-checksum.txt", BASE + "fake-checksum.txt.gpg")
    if scheme == CLEARSIGNED_CHECKSUM:
        return Release("fake", "1.0", "x86_64", "fake-1.0-x86_64.iso", iso_url, scheme, BASE + "FAKE-CHECKSUM")
    return Release("fake", "1.0", "x86_64", "fake-1.0-x86_64.iso", iso_url, scheme,
                   signature_url=iso_url + ".sig", size=len(FAKE_ISO), sha256=FAKE_SHA)


SIGNED_FILES = {BASE + name: fixture(name) for name in
                ("fake-checksum.txt", "fake-checksum.txt.gpg", "FAKE-CHECKSUM", "fake-1.0-x86_64.iso.sig")}


class FakeDownload:
    def __init__(self, content: bytes = FAKE_ISO):
        self.content = content
        self.calls = []

    def __call__(self, url, dest, *, expected_size=None, progress=None, should_stop=None, on_sha256=None):
        self.calls.append(url)
        Path(dest).write_bytes(self.content)
        if on_sha256 is not None:
            on_sha256(hashlib.sha256(self.content).hexdigest())
        return Path(dest)


def make_tools(tmp_path, files=None, content: bytes = FAKE_ISO):
    return manager.Tools(gpgv=GPGV, fetch=fake_fetch({**SIGNED_FILES, **(files or {})}),
                         download=FakeDownload(content), root=tmp_path / "iso")


@needs_gpgv
@pytest.mark.parametrize("scheme", [DETACHED_CHECKSUM, CLEARSIGNED_CHECKSUM, DETACHED_ISO])
def test_prepare_downloads_verifies_and_records(tmp_path, scheme):
    tools = make_tools(tmp_path)
    result = manager.prepare(fake_release(scheme), TEST_KEYS, tools)
    assert result.ok, result.verification.steps
    assert result.verification.sha256 == FAKE_SHA and result.verification.signer == TEST_FPR
    verified = json.loads((result.folder / iso_mod.VERIFIED_FILE).read_text())
    assert verified["sha256"] == FAKE_SHA and verified["signer"] == TEST_FPR
    saved = iso_mod.saved_releases("fake", tools.root)
    assert [s.release for s in saved] == [fake_release(scheme)] and saved[0].verified
    assert manager.reverify(saved[0], TEST_KEYS, tools).ok


@needs_gpgv
@pytest.mark.parametrize("scheme", [DETACHED_CHECKSUM, CLEARSIGNED_CHECKSUM, DETACHED_ISO])
def test_modified_iso_is_rejected_and_set_aside(tmp_path, scheme):
    tampered = bytearray(FAKE_ISO)
    tampered[1000] ^= 1
    tools = make_tools(tmp_path, content=bytes(tampered))
    result = manager.prepare(fake_release(scheme), TEST_KEYS, tools)
    assert not result.ok
    assert not (result.folder / "fake-1.0-x86_64.iso").exists()
    assert (result.folder / "fake-1.0-x86_64.iso.rejected").exists()
    assert not (result.folder / iso_mod.VERIFIED_FILE).exists()


@needs_gpgv
def test_checksum_signed_by_attacker_stops_before_the_big_download(tmp_path):
    tools = make_tools(tmp_path, {BASE + "fake-checksum.txt.gpg": fixture("fake-checksum.txt.evil.gpg")})
    result = manager.prepare(fake_release(DETACHED_CHECKSUM), TEST_KEYS, tools)
    assert not result.ok and "tidak di-pin" in result.verification.steps[0].message
    assert tools.download.calls == []


@needs_gpgv
def test_modified_checksum_file_stops_before_the_big_download(tmp_path):
    forged = fixture("fake-checksum.txt").replace(FAKE_SHA.encode(), b"f" * 64)
    tools = make_tools(tmp_path, {BASE + "fake-checksum.txt": forged})
    result = manager.prepare(fake_release(DETACHED_CHECKSUM), TEST_KEYS, tools)
    assert not result.ok and "TIDAK sah" in result.verification.steps[0].message
    assert tools.download.calls == []


@needs_gpgv
def test_text_outside_the_clearsigned_block_is_ignored(tmp_path):
    tampered = bytearray(FAKE_ISO)
    tampered[0] ^= 1
    evil_sha = hashlib.sha256(tampered).hexdigest()
    forged = fixture("FAKE-CHECKSUM") + f"SHA256 (fake-1.0-x86_64.iso) = {evil_sha}\n".encode()
    tools = make_tools(tmp_path, {BASE + "FAKE-CHECKSUM": forged}, content=bytes(tampered))
    result = manager.prepare(fake_release(CLEARSIGNED_CHECKSUM), TEST_KEYS, tools)
    assert not result.ok and result.verification.sha256 == FAKE_SHA


@needs_gpgv
def test_arch_iso_signed_by_attacker_is_rejected(tmp_path):
    """Ditandatangani kunci yang tidak di-pin: ditolak dan tidak bisa disalin. ISO-nya tidak disisihkan (bukan BADSIG),
    karena unduh ulang tidak menolong kalau masalahnya kunci; yang menentukan tetap verified.json + cek saat salin."""
    tools = make_tools(tmp_path, {BASE + "fake-1.0-x86_64.iso.sig": fixture("fake-1.0-x86_64.iso.evil.sig")})
    result = manager.prepare(fake_release(DETACHED_ISO), TEST_KEYS, tools)
    assert not result.ok and "tidak di-pin" in result.verification.steps[0].message
    assert not (result.folder / iso_mod.VERIFIED_FILE).exists()
    usb = tmp_path / "usb"
    usb.mkdir()
    saved = iso_mod.saved_releases("fake", tools.root)[0]
    assert not manager.copy_to_drive(saved, TEST_KEYS, tools, usb)[0].ok and not list(usb.iterdir())


@needs_gpgv
def test_existing_verified_iso_is_not_downloaded_again(tmp_path):
    tools = make_tools(tmp_path)
    manager.prepare(fake_release(DETACHED_CHECKSUM), TEST_KEYS, tools)
    again = manager.prepare(fake_release(DETACHED_CHECKSUM), TEST_KEYS, tools)
    assert again.ok and len(tools.download.calls) == 1 and "tidak diunduh ulang" in again.notes[0]


def test_tool_failure_does_not_discard_a_downloaded_iso(tmp_path):
    tools = manager.Tools(gpgv="gpgv", fetch=fake_fetch(SIGNED_FILES), download=FakeDownload(),
                          run_gpgv=fake_gpgv("", 2), root=tmp_path / "iso")
    result = manager.prepare(fake_release(DETACHED_ISO), TEST_KEYS, tools)
    assert not result.ok and (result.folder / "fake-1.0-x86_64.iso").exists()


# --- Unduhan yang bisa dilanjutkan (server HTTP lokal) ------------------------------------------------


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        server = self.server
        data = server.files.get(self.path)
        server.log.append((self.path, self.headers.get("Range")))
        if data is None:
            self.send_error(404)
            return
        start, status = 0, 200
        range_header = self.headers.get("Range")
        if range_header and not server.ignore_range:
            start = int(range_header.split("=")[1].rstrip("-"))
            if start >= len(data):
                self.send_error(416)
                return
            status = 206
        body = data[start:]
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
        self.end_headers()
        if server.drops > 0:
            server.drops -= 1
            self.wfile.write(body[: len(body) // 2])
            self.wfile.flush()
            self.close_connection = True
            return
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def http_server():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.files, server.log, server.drops, server.ignore_range = {}, [], 0, False
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server.url = f"http://127.0.0.1:{server.server_address[1]}"
    yield server
    server.shutdown()
    server.server_close()


BIG = bytes(range(256)) * 12_000  # ±3 MB, beberapa blok


def _get(server, dest, **kwargs):
    return download.download(server.url + "/x.iso", dest, require_https=False, backoff=0, **kwargs)


def test_download_whole_file_with_progress(http_server, tmp_path):
    http_server.files["/x.iso"] = BIG
    seen = []
    path = _get(http_server, tmp_path / "x.iso", expected_size=len(BIG), progress=lambda d, t: seen.append((d, t)))
    assert path.read_bytes() == BIG and not (tmp_path / "x.iso.part").exists()
    assert seen[-1] == (len(BIG), len(BIG))


def test_download_resumes_existing_part_file(http_server, tmp_path):
    http_server.files["/x.iso"] = BIG
    (tmp_path / "x.iso.part").write_bytes(BIG[:1_000_000])
    assert _get(http_server, tmp_path / "x.iso").read_bytes() == BIG
    assert http_server.log == [("/x.iso", "bytes=1000000-")]


def test_dropped_connection_is_resumed_automatically(http_server, tmp_path):
    http_server.files["/x.iso"] = BIG
    http_server.drops = 2
    assert _get(http_server, tmp_path / "x.iso", expected_size=len(BIG)).read_bytes() == BIG
    assert http_server.log[0][1] is None and all(r and r.startswith("bytes=") for _, r in http_server.log[1:])


def test_dropped_connection_beyond_retries_keeps_part_for_next_run(http_server, tmp_path):
    http_server.files["/x.iso"] = BIG
    http_server.drops = 10
    with pytest.raises(download.DownloadError, match="Jalankan perintahnya lagi"):
        _get(http_server, tmp_path / "x.iso", retries=1)
    assert 0 < (tmp_path / "x.iso.part").stat().st_size < len(BIG)
    http_server.drops = 0
    assert _get(http_server, tmp_path / "x.iso").read_bytes() == BIG


def test_server_without_range_support_restarts_from_zero(http_server, tmp_path):
    http_server.files["/x.iso"] = BIG
    http_server.ignore_range = True
    (tmp_path / "x.iso.part").write_bytes(b"sampah" * 1000)
    assert _get(http_server, tmp_path / "x.iso").read_bytes() == BIG


def test_size_different_from_feed_is_refused(http_server, tmp_path):
    http_server.files["/x.iso"] = BIG
    with pytest.raises(download.DownloadError, match="tidak sama dengan feed"):
        _get(http_server, tmp_path / "x.iso", expected_size=len(BIG) + 1)


def test_stop_request_keeps_part_file(http_server, tmp_path):
    http_server.files["/x.iso"] = BIG
    with pytest.raises(download.DownloadStopped):
        _get(http_server, tmp_path / "x.iso", should_stop=lambda: True)
    assert (tmp_path / "x.iso.part").exists() and not (tmp_path / "x.iso").exists()


def test_http_is_refused_and_fetch_has_a_size_limit(http_server, tmp_path):
    with pytest.raises(download.DownloadError, match="HTTPS"):
        download.download(http_server.url + "/x.iso", tmp_path / "x.iso")
    http_server.files["/small"] = b"x" * 100
    assert download.fetch(http_server.url + "/small", require_https=False) == b"x" * 100
    with pytest.raises(download.DownloadError, match="lebih besar"):
        download.fetch(http_server.url + "/small", require_https=False, limit=10)
    with pytest.raises(download.DownloadError, match="404"):
        download.fetch(http_server.url + "/missing", require_https=False, backoff=0)


# --- USB Ventoy & salin ----------------------------------------------------------------------------


def test_scan_isos_on_drive(tmp_path):
    for rel in ("Fedora-Workstation-Live-43-1.6.x86_64.iso", "Linux/archlinux-2026.09.01-x86_64.iso",
                "a/b/ubuntu-24.04.2-desktop-amd64.iso", "linuxmint-22.3-xfce-64bit.iso", "random.iso",
                "ventoy/archlinux-2099.01.01-x86_64.iso", "a/b/c/d/linuxmint-99-mate-64bit.iso"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_bytes(b"x")
    found = {(i.distro, i.version, i.edition) for i in catalog.scan_isos(tmp_path, sources.SOURCES)}
    assert found == {("fedora", "43", "Workstation"), ("arch", "2026.09.01", "x86_64"),
                     ("ubuntu", "24.04.2", "desktop"), ("mint", "22.3", "xfce")}


def test_catalog_status_against_drive(tmp_path):
    drive = tmp_path / "usb"
    drive.mkdir()
    for name in ("Fedora-Workstation-Live-43-1.6.x86_64.iso", "linuxmint-22.3-cinnamon-64bit.iso",
                 "archlinux-2026.10.01-x86_64.iso"):
        (drive / name).write_bytes(b"x")
    rows = {r.distro: r for r in catalog.check(sources.SOURCES, fake_fetch(FEEDS), drive=drive,
                                                downloads_root=tmp_path / "dl")}
    assert rows["fedora"].status == catalog.NEW
    assert rows["mint"].status == catalog.CURRENT and rows["arch"].status == catalog.CURRENT
    assert rows["ubuntu"].status == catalog.MISSING


def test_ventoy_detection(tmp_path):
    usb = catalog.Volume("E", "Ventoy", "exFAT", True, False)
    other = catalog.Volume("F", "DATA", "NTFS", False, False)
    system = catalog.Volume("C", "Ventoy", "NTFS", False, True)
    assert catalog.ventoy_volumes([usb, other, system]) == [usb]


def test_copy_verified_writes_and_reads_back(tmp_path):
    source = tmp_path / "src" / "fake-1.0-x86_64.iso"
    source.parent.mkdir()
    source.write_bytes(FAKE_ISO)
    usb = tmp_path / "usb"
    usb.mkdir()
    steps = manager.copy_verified(source, usb, FAKE_SHA)
    assert all(s.ok for s in steps) and (usb / source.name).read_bytes() == FAKE_ISO
    assert not list(usb.glob("*" + manager.PART_SUFFIX))
    again = manager.copy_verified(source, usb, FAKE_SHA)
    assert again[0].ok and "identik" in again[0].message


def test_copy_refuses_overwrite_wrong_hash_fat32_and_full_drive(tmp_path, monkeypatch):
    source = tmp_path / "fake-1.0-x86_64.iso"
    source.write_bytes(FAKE_ISO)
    usb = tmp_path / "usb"
    usb.mkdir()
    (usb / source.name).write_bytes(b"isi lain")
    assert "tidak ditimpa" in manager.copy_verified(source, usb, FAKE_SHA)[0].message
    assert (usb / source.name).read_bytes() == b"isi lain"
    (usb / source.name).unlink()

    wrong = manager.copy_verified(source, usb, "0" * 64)
    assert not wrong[0].ok and not list(usb.iterdir())
    monkeypatch.setattr(manager, "FAT32_LIMIT", 10)
    assert "FAT32" in manager.copy_verified(source, usb, FAKE_SHA, filesystem="FAT32")[0].message
    assert manager.copy_verified(source, usb, FAKE_SHA, filesystem="exFAT")[0].ok
    (usb / source.name).unlink()
    full = manager.copy_verified(source, usb, FAKE_SHA, disk_usage=lambda p: (0, 0, 1000))
    assert "ruang kosong" in full[0].message and not list(usb.iterdir())


@needs_gpgv
def test_copy_rechecks_signature_right_before_copying(tmp_path):
    tools = make_tools(tmp_path)
    result = manager.prepare(fake_release(DETACHED_CHECKSUM), TEST_KEYS, tools)
    saved = iso_mod.saved_releases("fake", tools.root)[0]
    usb = tmp_path / "usb"
    usb.mkdir()
    (result.folder / "fake-checksum.txt").write_bytes(fixture("fake-checksum.txt") + b"\n")
    steps = manager.copy_to_drive(saved, TEST_KEYS, tools, usb)
    assert not steps[0].ok and not list(usb.iterdir())
    (result.folder / "fake-checksum.txt").write_bytes(fixture("fake-checksum.txt"))
    steps = manager.copy_to_drive(saved, TEST_KEYS, tools, usb)
    assert all(s.ok for s in steps), steps
    assert (usb / "fake-1.0-x86_64.iso").read_bytes() == FAKE_ISO


# --- CLI ----------------------------------------------------------------------------------------


def iso_args(command, distro=None, **extra):
    return Namespace(iso_command=command, distro=distro, edition=None, iso_version=None, drive=None, **extra)


def test_cli_check_without_ventoy(tmp_path, capsys):
    from distrorelay.__main__ import _run_iso_command

    tools = manager.Tools(gpgv="", fetch=fake_fetch(FEEDS), root=tmp_path)
    code = _run_iso_command(iso_args("check"), tools=tools, volumes=lambda: [])
    out = capsys.readouterr().out
    assert code == 0 and "44 (Workstation" in out and "tidak terdeteksi" in out


def test_cli_download_needs_gpgv(tmp_path, capsys):
    from distrorelay.__main__ import _run_iso_command

    tools = manager.Tools(gpgv="", fetch=fake_fetch(FEEDS), root=tmp_path)
    assert _run_iso_command(iso_args("download", "mint"), tools=tools) == 1
    assert "winget install GnuPG.GnuPG" in capsys.readouterr().out


@needs_gpgv
def test_cli_download_real_mint_signature_but_wrong_iso_is_rejected(tmp_path, capsys):
    """Tanda tangan sha256sum.txt Mint asli lolos dengan kunci bawaan, tapi ISO palsu ditolak SHA256-nya."""
    from distrorelay.__main__ import _run_iso_command

    files = {**FEEDS, mint.BASE + "22.3/sha256sum.txt.gpg": fixture("sha256sum.txt.gpg")}
    tools = manager.Tools(gpgv=GPGV, fetch=fake_fetch(files), download=FakeDownload(b"bukan ISO asli"), root=tmp_path)
    code = _run_iso_command(iso_args("download", "mint"), tools=tools)
    out = capsys.readouterr().out
    assert code == 1 and "Linux Mint ISO Signing Key" in out and "TIDAK lolos" in out
    assert (tmp_path / "mint" / "22.3-cinnamon" / "linuxmint-22.3-cinnamon-64bit.iso.rejected").exists()


def test_cli_copy_without_download(tmp_path, capsys):
    from distrorelay.__main__ import _run_iso_command

    tools = manager.Tools(gpgv="gpgv", fetch=fake_fetch(FEEDS), root=tmp_path)
    assert _run_iso_command(iso_args("copy", "fedora"), tools=tools, volumes=lambda: []) == 1
    assert "Belum ada unduhan" in capsys.readouterr().out
