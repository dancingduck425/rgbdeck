# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 dancingduck425
"""
Finds out whether a newer RGB Deck has been released and, where this installation can be replaced safely,
installs it.

What is asked: one HTTPS request to GitHub's public API for this project's releases (no account, nothing about
the user is sent beyond what any web request carries). When: at every start and once a day while the app keeps
running, if the setting is on.

How an update is applied depends on how this copy was installed:

    windows-setup      installed with RGBDeck-Setup-x.exe: the new setup program is downloaded, checked against
                       the SHA-256 GitHub publishes for it, and run silently after the app has closed
    windows-portable   a lone RGBDeck.exe: the new exe is downloaded and checked the same way, the running one is
                       renamed aside (Windows allows that) and the new one takes its name
    git                a git checkout: `git pull --ff-only` (refused when there are local changes)
    source             an unpacked source folder (install.sh): the release's source archive is downloaded and the
                       `rgbdeck` package folder is swapped, with the old one put back if anything fails
    venv               installed with pip / pipx into its own environment: pip installs the release there
    managed            a system package (pacman, /usr/local): the updater only says so and links to the release

Everything is downloaded from this project's GitHub repository only; other addresses are refused.
Qt is not used here: the dialog lives in app.py, and tests run this module against a stand-in server.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from rgbdeck import __version__

log = logging.getLogger("rgbdeck.updater")

REPO = "dancingduck425/rgbdeck"
API = f"https://api.github.com/repos/{REPO}/releases?per_page=15"
TRUSTED = (f"https://github.com/{REPO}/", f"https://api.github.com/repos/{REPO}/")
CHECK_EVERY = 20 * 3600  # seconds between automatic checks
SELF_UPDATING = ("windows-setup", "windows-portable", "git", "source", "venv")
Progress = Optional[Callable[[str, int], None]]  # (what is happening, percent or -1)


class UpdateError(Exception):
    """Something the user should read: the update did not happen, the installation is as it was."""


class Asset:
    def __init__(self, name: str, url: str, size: int, sha256: Optional[str]):
        self.name, self.url, self.size, self.sha256 = name, url, size, sha256


class Release:
    def __init__(self, version: str, tag: str, notes: str, page: str, tarball: str, assets: Dict[str, Asset]):
        self.version, self.tag, self.notes, self.page, self.tarball, self.assets = (
            version,
            tag,
            notes,
            page,
            tarball,
            assets,
        )

    def asset(self, kind: str) -> Optional[Asset]:
        """The download this kind of installation needs (None: nothing to download, or not uploaded yet)."""
        if kind == "windows-setup":
            return next(
                (a for n, a in self.assets.items() if n.lower().startswith("rgbdeck-setup") and n.endswith(".exe")),
                None,
            )
        if kind == "windows-portable":
            return self.assets.get("RGBDeck.exe")
        return None


# ----------------------------------------------------------------------------- versions
def parse_version(text: str) -> Tuple[int, ...]:
    """ "v1.8.2" -> (1, 8, 2). Anything that is not plain numbers compares as older than everything."""
    m = re.fullmatch(r"v?(\d+(?:\.\d+)*)", (text or "").strip())
    return tuple(int(x) for x in m.group(1).split(".")) if m else ()


def is_newer(candidate: str, current: str) -> bool:
    a, b = parse_version(candidate), parse_version(current)
    n = max(len(a), len(b))
    return bool(a) and a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


def current_version() -> str:
    return os.environ.get("RGBDECK_PRETEND_VERSION") or __version__  # the override exists to try the dialog out


# ----------------------------------------------------------------------------- asking GitHub
def _trusted(url: str) -> bool:
    return any(url.startswith(prefix) for prefix in TRUSTED)


def _open(url: str, timeout: float):
    import urllib.request  # here: only needed when a check or a download actually happens

    if not _trusted(url):
        raise UpdateError(f"refusing to fetch {url}: not this project's GitHub repository")
    req = urllib.request.Request(
        url, headers={"User-Agent": f"rgbdeck/{__version__}", "Accept": "application/vnd.github+json"}
    )
    return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310 - https, host checked above


def parse_releases(data: list, current: str) -> Optional[Release]:
    """The newest published release that is newer than `current`, with the notes of every release in between
    (so somebody who skipped two versions reads about all three). None: up to date."""
    newer = []
    for r in data if isinstance(data, list) else []:
        if not isinstance(r, dict) or r.get("draft") or r.get("prerelease"):
            continue
        tag = str(r.get("tag_name") or "")
        if is_newer(tag, current):
            newer.append((parse_version(tag), r))
    if not newer:
        return None
    newer.sort(key=lambda item: item[0], reverse=True)
    top = newer[0][1]
    notes = []
    for version, r in newer:
        body = str(r.get("body") or "").strip() or "_No notes for this version._"
        notes.append(f"## {'.'.join(map(str, version))}\n\n{body}" if len(newer) > 1 else body)
    assets = {}
    for a in top.get("assets") or []:
        url = str(a.get("browser_download_url") or "")
        digest = str(a.get("digest") or "")
        if a.get("name") and _trusted(url):
            sha = digest[7:].lower() if digest.startswith("sha256:") else None
            assets[str(a["name"])] = Asset(str(a["name"]), url, int(a.get("size") or 0), sha)
    tag = str(top["tag_name"])
    return Release(
        ".".join(map(str, newer[0][0])),
        tag,
        "\n\n".join(notes),
        str(top.get("html_url") or f"https://github.com/{REPO}/releases/tag/{tag}"),
        f"https://github.com/{REPO}/archive/refs/tags/{tag}.tar.gz",
        assets,
    )


def check(current: Optional[str] = None, timeout: float = 10.0) -> Optional[Release]:
    """Ask GitHub. A Release when there is a newer one, None when up to date; UpdateError when it cannot say."""
    try:
        with _open(API, timeout) as resp:
            data = json.loads(resp.read(4_000_000))
    except UpdateError:
        raise
    except Exception as e:  # noqa: BLE001 - offline, rate limited, GitHub down: all the same to the user
        raise UpdateError(f"could not reach GitHub ({e})") from e
    return parse_releases(data, current or current_version())


# ----------------------------------------------------------------------------- this installation
def package_dir() -> Path:
    return Path(__file__).resolve().parent


def install_kind(package: Optional[Path] = None, frozen: Optional[bool] = None, exe: Optional[Path] = None) -> str:
    """How this copy was installed (see the module text). The arguments exist for tests."""
    frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    if frozen:
        exe = Path(exe or sys.executable)
        return "windows-setup" if any(exe.parent.glob("unins*.exe")) else "windows-portable"
    package = Path(package or package_dir())
    root = package.parent
    if (root / ".git").exists() and shutil.which("git"):
        return "git"
    in_site = any(part in ("site-packages", "dist-packages") for part in package.parts)
    if in_site:
        in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
        return "venv" if in_venv and os.access(package, os.W_OK) else "managed"
    if os.access(root, os.W_OK) and os.access(package, os.W_OK) and (root / "install.sh").exists():
        return "source"
    return "managed"


def how_to_update_by_hand(kind: str) -> str:
    if kind == "managed":
        return (
            "This copy was installed by a package manager or system-wide, so RGB Deck can't replace it itself. "
            "Update it the way you installed it (for example your distribution's package, or run install.sh from "
            "the new release)."
        )
    return ""


# ----------------------------------------------------------------------------- downloading
def download(
    asset_url: str,
    dest: Path,
    size: int = 0,
    sha256: Optional[str] = None,
    progress: Progress = None,
    what: str = "Downloading",
):
    """Fetch a file from the project's GitHub repository and check it. The file only appears under its final
    name when it is complete and matches the size and SHA-256 GitHub lists for it."""
    import hashlib

    part = dest.with_name(dest.name + ".part")
    digest = hashlib.sha256()
    got = 0
    try:
        with _open(asset_url, 30.0) as resp, open(part, "wb") as out:
            total = size or int(resp.headers.get("Content-Length") or 0)
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                digest.update(chunk)
                got += len(chunk)
                if progress:
                    progress(what, min(99, got * 100 // total) if total else -1)
        if size and got != size:
            raise UpdateError(f"the download is incomplete ({got} of {size} bytes)")
        if sha256 and digest.hexdigest() != sha256.lower():
            raise UpdateError("the download does not match the checksum GitHub lists for it; nothing was installed")
        os.replace(part, dest)
    except UpdateError:
        part.unlink(missing_ok=True)
        raise
    except Exception as e:  # noqa: BLE001
        part.unlink(missing_ok=True)
        raise UpdateError(f"the download failed ({e})") from e


# ----------------------------------------------------------------------------- applying
def _run(cmd: List[str], cwd: Optional[Path] = None, timeout: float = 180.0) -> str:
    flags = {"creationflags": 0x08000000} if sys.platform == "win32" else {}  # no console window
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, **flags)
    except (OSError, subprocess.SubprocessError) as e:
        raise UpdateError(f"{cmd[0]} could not be run ({e})") from e
    if p.returncode != 0:
        tail = (p.stderr or p.stdout or "").strip().splitlines()[-3:]
        raise UpdateError(f"{' '.join(cmd[:3])} failed: {' / '.join(tail) or 'no message'}")
    return p.stdout


def _apply_git(release: Release, root: Path, progress: Progress):
    if progress:
        progress("Checking the git checkout", -1)
    if _run(["git", "status", "--porcelain", "--untracked-files=no"], root).strip():
        raise UpdateError(
            "this git checkout has local changes, so it was left alone. Commit or stash them and run 'git pull'."
        )
    if progress:
        progress("Pulling the new version", -1)
    _run(["git", "pull", "--ff-only", "--tags"], root)


def _apply_source(release: Release, package: Path, progress: Progress):
    """Swap the package folder for the one in the release's source archive; the old folder comes back if
    anything goes wrong before the new one is in place."""
    root = package.parent
    work = Path(tempfile.mkdtemp(prefix="rgbdeck-update-", dir=root))
    old = root / f".{package.name}.previous"
    try:
        archive = work / "source.tar.gz"
        download(release.tarball, archive, progress=progress)
        if progress:
            progress("Unpacking", -1)
        with tarfile.open(archive) as tar:
            top = {m.name.split("/", 1)[0] for m in tar.getmembers()}
            if len(top) != 1:
                raise UpdateError("the source archive has an unexpected layout")
            try:
                tar.extractall(work, filter="data")  # refuses paths outside, links, devices
            except TypeError:  # Python older than 3.12 without the filter argument
                for m in tar.getmembers():
                    target = (work / m.name).resolve()
                    if not str(target).startswith(str(work.resolve())) or m.issym() or m.islnk() or m.isdev():
                        raise UpdateError("the source archive contains unsafe paths") from None
                tar.extractall(work)  # noqa: S202 - members checked above
        new = work / top.pop() / package.name
        text = (new / "__init__.py").read_text() if (new / "__init__.py").exists() else ""
        found = re.search(r'__version__\s*=\s*"([^"]+)"', text)
        if not found or parse_version(found.group(1)) != parse_version(release.version):
            raise UpdateError("the source archive does not contain the version it claims to be")
        if progress:
            progress("Installing", -1)
        shutil.rmtree(old, ignore_errors=True)
        os.rename(package, old)
        try:
            os.rename(new, package)
        except OSError:
            os.rename(old, package)  # put the working version back
            raise
        shutil.rmtree(old, ignore_errors=True)
        for extra in ("install.sh", "README.md", "CHANGELOG.md", "LICENSE"):  # keep the folder's own files current
            src = new.parent / extra
            if src.exists():
                try:
                    shutil.copy2(src, root / extra)
                except OSError:
                    pass
    except UpdateError:
        raise
    except Exception as e:  # noqa: BLE001
        raise UpdateError(f"could not replace the program files ({e})") from e
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _apply_venv(release: Release, progress: Progress):
    if progress:
        progress("Installing with pip", -1)
    _run([sys.executable, "-m", "pip", "install", "--upgrade", "--no-input", release.tarball], timeout=600.0)


def _apply_portable(release: Release, exe: Path, progress: Progress):
    asset = release.asset("windows-portable")
    if asset is None:
        raise UpdateError("this release has no RGBDeck.exe yet (it is uploaded a few minutes after the release)")
    new, old = exe.with_name(exe.name + ".new"), exe.with_name(exe.name + ".old")
    download(asset.url, new, asset.size, asset.sha256, progress)
    try:
        old.unlink(missing_ok=True)
        os.rename(exe, old)  # a running program can be renamed on Windows, just not overwritten
        try:
            os.rename(new, exe)
        except OSError:
            os.rename(old, exe)
            raise
    except OSError as e:
        new.unlink(missing_ok=True)
        raise UpdateError(f"could not replace {exe.name} ({e}). Is its folder writable?") from e


def _prepare_setup(release: Release, exe: Path, progress: Progress) -> List[str]:
    asset = release.asset("windows-setup")
    if asset is None:
        raise UpdateError("this release has no setup program yet (it is uploaded a few minutes after the release)")
    folder = Path(tempfile.gettempdir()) / "rgbdeck-update"
    folder.mkdir(exist_ok=True)
    setup = folder / asset.name
    download(asset.url, setup, asset.size, asset.sha256, progress)
    # Run the setup once this program has closed, then start the new version. Silent mode skips the
    # "launch RGB Deck" step of the installer, hence the explicit start.
    script = f'start "" /wait "{setup}" /SILENT /SUPPRESSMSGBOXES /NORESTART /CLOSEAPPLICATIONS & start "" "{exe}"'
    return ["cmd.exe", "/c", script]


def apply(
    release: Release,
    kind: Optional[str] = None,
    progress: Progress = None,
    relaunch: Optional[List[str]] = None,
    package: Optional[Path] = None,
    exe: Optional[Path] = None,
) -> List[str]:
    """Install the release. Returns the command to run once this process has exited (it starts the new
    version; for windows-setup it runs the installer first). Raises UpdateError with the installation unchanged."""
    kind = kind or install_kind()
    package = Path(package or package_dir())
    exe = Path(exe or sys.executable)
    relaunch = list(relaunch or [])
    log.info("updating %s -> %s (%s installation)", current_version(), release.version, kind)
    if kind == "git":
        _apply_git(release, package.parent, progress)
    elif kind == "source":
        _apply_source(release, package, progress)
    elif kind == "venv":
        _apply_venv(release, progress)
    elif kind == "windows-portable":
        _apply_portable(release, exe, progress)
    elif kind == "windows-setup":
        return _prepare_setup(release, exe, progress)
    else:
        raise UpdateError(how_to_update_by_hand("managed"))
    if progress:
        progress("Restarting", 100)
    return relaunch


def cleanup_leftovers(package: Optional[Path] = None, exe: Optional[Path] = None):
    """At start: remove what a finished update left behind (the previous exe, a previous package folder)."""
    try:
        if getattr(sys, "frozen", False) or exe is not None:
            target = Path(exe or sys.executable)
            target.with_name(target.name + ".old").unlink(missing_ok=True)
            target.with_name(target.name + ".new").unlink(missing_ok=True)
        package = Path(package or package_dir())
        previous = package.parent / f".{package.name}.previous"
        if previous.exists() and package.exists():
            shutil.rmtree(previous, ignore_errors=True)
    except OSError as e:
        log.debug("update leftovers not removed: %s", e)
