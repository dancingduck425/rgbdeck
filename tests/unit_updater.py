"""The updater against a stand-in for GitHub: version logic, what kind of installation this is, checksum-verified
downloads, and real updates of a git checkout, a source folder and a "portable exe" in temporary directories."""

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rgbdeck import updater  # noqa: E402

fails = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


# --- versions
check(updater.parse_version("v1.8.2") == (1, 8, 2) and updater.parse_version("2.0") == (2, 0), "version tags are read")
check(
    updater.parse_version("nightly") == () and updater.parse_version("v1.2-beta") == (), "anything else is no version"
)
check(
    updater.is_newer("v1.9.0", "1.8.2") and updater.is_newer("1.10.0", "1.9.9") and updater.is_newer("2.0", "1.99.99"),
    "newer is newer",
)
check(
    not updater.is_newer("1.8.2", "1.8.2")
    and not updater.is_newer("1.8", "1.8.0")
    and not updater.is_newer("1.7.9", "1.8.0"),
    "same or older is not",
)
check(not updater.is_newer("garbage", "1.0.0"), "a tag that is no version never counts as an update")

# --- a stand-in for GitHub
FILES = {}
API_DATA = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = json.dumps(API_DATA).encode() if self.path.startswith("/api") else FILES.get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


srv = HTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"
updater.API = BASE + "/api"
updater.TRUSTED = (BASE + "/",)


def release(tag, body, assets=(), **extra):
    return {
        "tag_name": tag,
        "draft": False,
        "prerelease": False,
        "body": body,
        "html_url": f"{BASE}/page/{tag}",
        "assets": [
            {
                "name": n,
                "browser_download_url": f"{BASE}/dl/{tag}/{n}",
                "size": len(d),
                "digest": "sha256:" + hashlib.sha256(d).hexdigest(),
            }
            for n, d in assets
        ],
        **extra,
    }


exe_new = b"MZ new program " * 5000
API_DATA[:] = [
    release("v9.9.9", "from the future", draft=True),
    release("v2.1.0-rc1", "release candidate", prerelease=True),
    release("v2.0.0", "- big thing", [("RGBDeck.exe", exe_new), ("RGBDeck-Setup-2.0.0.exe", b"setup" * 100)]),
    release("v1.9.0", "- middle thing"),
    release("v1.8.2", "- what you have"),
    {"tag_name": "weird"},
]
for r in API_DATA:
    for a in r.get("assets", []):
        FILES["/dl/" + a["browser_download_url"].split("/dl/")[1]] = (
            exe_new if a["name"] == "RGBDeck.exe" else b"setup" * 100
        )
rel = updater.check("1.8.2")
check(
    rel is not None and rel.version == "2.0.0" and rel.tag == "v2.0.0",
    "the newest published release is offered (drafts and pre-releases are not)",
)
check(
    "## 2.0.0" in rel.notes
    and "- big thing" in rel.notes
    and "## 1.9.0" in rel.notes
    and "what you have" not in rel.notes,
    "notes cover every version since yours",
)
check(updater.check("1.9.0").notes == "- big thing", "one version behind: just its notes")
check(updater.check("2.0.0") is None and updater.check("3.0") is None, "up to date: nothing offered")
check(
    rel.asset("windows-portable").name == "RGBDeck.exe"
    and rel.asset("windows-setup").name == "RGBDeck-Setup-2.0.0.exe",
    "downloads are picked per kind of installation",
)
check(
    updater.check("1.8.2").asset("git") is None
    and updater.parse_releases(API_DATA[3:4], "1.0").asset("windows-setup") is None,
    "no download for kinds that need none / not uploaded yet",
)
os.environ["RGBDECK_PRETEND_VERSION"] = "0.1.0"
check(updater.current_version() == "0.1.0", "the version can be pretended (to try the dialog)")
del os.environ["RGBDECK_PRETEND_VERSION"]
saved_api = updater.API
updater.API = "http://127.0.0.1:9/api"
try:
    updater.check("1.0")
    check(False, "unreachable server raises")
except updater.UpdateError as e:
    check("refusing" in str(e) or "could not reach" in str(e), f"no answer is reported, not fatal ({str(e)[:50]})")
updater.API = saved_api

# --- downloads are verified
tmp = Path(tempfile.mkdtemp(prefix="rgbdeck-upd-"))
a = rel.asset("windows-portable")
steps = []
updater.download(a.url, tmp / "ok.exe", a.size, a.sha256, lambda text, pct: steps.append(pct))
check(
    (tmp / "ok.exe").read_bytes() == exe_new and steps and steps[-1] <= 99,
    "a matching download is kept; progress is reported",
)
for label, kwargs in (("wrong checksum", {"sha256": "0" * 64}), ("wrong size", {"size": a.size + 1})):
    try:
        updater.download(a.url, tmp / "bad.exe", **{"size": a.size, "sha256": a.sha256, **kwargs})
        check(False, f"{label} is refused")
    except updater.UpdateError:
        check(
            not (tmp / "bad.exe").exists() and not (tmp / "bad.exe.part").exists(),
            f"{label}: refused, nothing left behind",
        )
try:
    updater.download("https://example.com/evil.exe", tmp / "evil.exe")
    check(False, "foreign address refused")
except updater.UpdateError as e:
    check("refusing" in str(e), "an address outside the project is refused")

# --- which kind of installation
src = tmp / "srcinstall"
(src / "rgbdeck").mkdir(parents=True)
(src / "install.sh").write_text("#!/bin/sh\n")
(src / "rgbdeck" / "__init__.py").write_text('__version__ = "1.8.2"\n')
(src / "rgbdeck" / "old_only.py").write_text("x = 1\n")
check(updater.install_kind(src / "rgbdeck", frozen=False) == "source", "a writable source folder with install.sh")
site = tmp / "lib" / "python3" / "site-packages" / "rgbdeck"
site.mkdir(parents=True)
check(updater.install_kind(site, frozen=False) in ("managed", "venv"), "inside site-packages: pip's business")
bare = tmp / "elsewhere" / "rgbdeck"
bare.mkdir(parents=True)
check(updater.install_kind(bare, frozen=False) == "managed", "an unknown layout is left to the user")
port = tmp / "portable"
port.mkdir()
(port / "RGBDeck.exe").write_bytes(b"MZ old program")
check(updater.install_kind(frozen=True, exe=port / "RGBDeck.exe") == "windows-portable", "a lone exe is portable")
(port / "unins000.exe").write_bytes(b"x")
check(
    updater.install_kind(frozen=True, exe=port / "RGBDeck.exe") == "windows-setup",
    "an exe next to its uninstaller was set up",
)
(port / "unins000.exe").unlink()
check(
    updater.how_to_update_by_hand("managed") and not updater.how_to_update_by_hand("git"),
    "managed installs get an explanation",
)


# --- source folder: the package is swapped for the one in the release archive
def source_archive(version, top="rgbdeck-2.0.0", evil=False):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:

        def add(name, text):
            data = text.encode()
            info = tarfile.TarInfo(f"{top}/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))

        add("rgbdeck/__init__.py", f'__version__ = "{version}"\n')
        add("rgbdeck/new_only.py", "y = 2\n")
        add("install.sh", "#!/bin/sh\n# new\n")
        if evil:
            info = tarfile.TarInfo(f"{top}/../../escaped.txt")
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))
    return buf.getvalue()


FILES["/archive/v2.0.0.tar.gz"] = source_archive("2.0.0")
rel.tarball = BASE + "/archive/v2.0.0.tar.gz"
cmd = updater.apply(rel, "source", relaunch=["start-it"], package=src / "rgbdeck")
check(cmd == ["start-it"], "after a source update the app is started again")
check(
    '"2.0.0"' in (src / "rgbdeck" / "__init__.py").read_text() and (src / "rgbdeck" / "new_only.py").exists(),
    "the new package is in place",
)
check(
    not (src / "rgbdeck" / "old_only.py").exists() and "# new" in (src / "install.sh").read_text(),
    "old files are gone, install.sh refreshed",
)
check(
    sorted(p.name for p in src.iterdir()) == ["install.sh", "rgbdeck"],
    f"nothing is left behind ({sorted(p.name for p in src.iterdir())})",
)
for label, data in (
    ("wrong version inside", source_archive("1.0.0")),
    ("unsafe paths", source_archive("2.0.0", evil=True)),
    ("not an archive", b"<html>rate limited</html>"),
):
    FILES["/archive/v2.0.0.tar.gz"] = data
    (src / "rgbdeck" / "__init__.py").write_text('__version__ = "1.8.2"\n')
    try:
        updater.apply(rel, "source", package=src / "rgbdeck")
        check(False, f"{label}: refused")
    except updater.UpdateError:
        intact = '"1.8.2"' in (src / "rgbdeck" / "__init__.py").read_text() and sorted(
            p.name for p in src.iterdir()
        ) == ["install.sh", "rgbdeck"]
        check(intact and not (tmp / "escaped.txt").exists(), f"{label}: refused, the installation is untouched")

# --- portable exe: the running one is renamed aside, the verified new one takes its place
cmd = updater.apply(rel, "windows-portable", relaunch=[str(port / "RGBDeck.exe")], exe=port / "RGBDeck.exe")
check(
    (port / "RGBDeck.exe").read_bytes() == exe_new and (port / "RGBDeck.exe.old").read_bytes() == b"MZ old program",
    "portable: new exe in place, old one kept aside until the restart",
)
updater.cleanup_leftovers(package=src / "rgbdeck", exe=port / "RGBDeck.exe")
check(not (port / "RGBDeck.exe.old").exists(), "the next start clears the old exe away")
FILES["/dl/v2.0.0/RGBDeck.exe"] = b"tampered"
(port / "RGBDeck.exe").write_bytes(b"MZ old program")
try:
    updater.apply(rel, "windows-portable", exe=port / "RGBDeck.exe")
    check(False, "tampered exe refused")
except updater.UpdateError:
    check(
        (port / "RGBDeck.exe").read_bytes() == b"MZ old program" and not (port / "RGBDeck.exe.new").exists(),
        "a tampered exe is refused and the old one stays",
    )
FILES["/dl/v2.0.0/RGBDeck.exe"] = exe_new
cmd = updater.apply(rel, "windows-setup", exe=port / "RGBDeck.exe")
check(
    cmd[:2] == ["cmd.exe", "/c"]
    and "/SILENT" in cmd[2]
    and "RGBDeck-Setup-2.0.0.exe" in cmd[2]
    and str(port / "RGBDeck.exe") in cmd[2],
    "setup: the verified installer runs after exit, then the app starts",
)
try:
    updater.apply(updater.parse_releases(API_DATA[3:4], "1.0"), "windows-setup", exe=port / "RGBDeck.exe")
    check(False, "missing asset")
except updater.UpdateError as e:
    check("uploaded" in str(e), "a release whose downloads are not up yet says so")
try:
    updater.apply(rel, "managed")
    check(False, "managed refused")
except updater.UpdateError:
    check(True, "a package-managed installation is never touched")

# --- git checkout: pull, unless there are local changes
if shutil.which("git"):

    def git(*args, cwd):
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "init.defaultBranch=main", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
        )

    origin, work, clone = tmp / "origin.git", tmp / "work", tmp / "clone"
    git("init", "--bare", str(origin), cwd=tmp)
    git("init", str(work), cwd=tmp)
    (work / "rgbdeck").mkdir()
    (work / "rgbdeck" / "__init__.py").write_text('__version__ = "1.8.2"\n')
    git("add", ".", cwd=work)
    git("commit", "-m", "1.8.2", cwd=work)
    git("branch", "-M", "main", cwd=work)
    git("remote", "add", "origin", str(origin), cwd=work)
    git("push", "-u", "origin", "main", cwd=work)
    git("clone", "-b", "main", str(origin), str(clone), cwd=tmp)
    check(updater.install_kind(clone / "rgbdeck", frozen=False) == "git", "a git checkout is recognised")
    (work / "rgbdeck" / "__init__.py").write_text('__version__ = "2.0.0"\n')
    git("commit", "-am", "2.0.0", cwd=work)
    git("push", "origin", "main", cwd=work)
    (clone / "rgbdeck" / "__init__.py").write_text('__version__ = "1.8.2"  # my tweak\n')
    try:
        updater.apply(rel, "git", package=clone / "rgbdeck")
        check(False, "dirty checkout refused")
    except updater.UpdateError as e:
        check(
            "local changes" in str(e) and "my tweak" in (clone / "rgbdeck" / "__init__.py").read_text(),
            "git: local changes are left alone, with an explanation",
        )
    git("checkout", "--", ".", cwd=clone)
    cmd = updater.apply(rel, "git", relaunch=["start-it"], package=clone / "rgbdeck")
    check(
        cmd == ["start-it"] and '"2.0.0"' in (clone / "rgbdeck" / "__init__.py").read_text(),
        "git: pulled to the new version",
    )
else:
    print("SKIP git tests (git not installed)")

srv.shutdown()
shutil.rmtree(tmp, ignore_errors=True)
print("\nALL PASSED" if not fails else "\nFAILURES: " + str(fails))
sys.exit(1 if fails else 0)
