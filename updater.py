# -*- coding: utf-8 -*-
"""Check GitHub for a newer release and swap the frozen build in place.

Pure stdlib so it adds nothing to the PyInstaller spec and stays unit-testable
without a network. The Flask routes in app.py wrap these; this module holds the
version, network and self-replace mechanics.

Self-update only applies to the frozen onefile build. Running from source,
is_frozen() is False and stage_and_swap() refuses - there is no exe to replace,
and the right upgrade path is git pull.

Unlike a bare-exe updater, the CMIS release asset is a zip holding the exe next
to the operation manual and its images, so the whole payload is extracted and
swapped together; shipping a new exe beside last version's manual would leave
the two disagreeing about the tool's own behaviour.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import ssl
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from typing import Callable, Optional

GITHUB_OWNER = "zhh198903-ctrl"
GITHUB_REPO = "cmis-module-manager"
# Release assets are versioned (CMIS_dist_v2_0_1.zip), so match the shape.
ASSET_PATTERN = re.compile(r"^CMIS_dist_v[\d_]+\.zip$", re.I)
EXE_NAME = "CMIS_Module_Manager.exe"
_USER_AGENT = "CMIS-Module-Manager-Updater"  # GitHub REST requires a User-Agent

# Where a release asset may legitimately live. GitHub answers the download URL
# with a redirect to its object store, so the whole chain is checked, not just
# the first hop: urllib follows redirects on its own and would just as happily
# follow one onto plain http, or off GitHub entirely.
_TRUSTED_HOSTS = frozenset(['github.com', 'api.github.com'])
_TRUSTED_SUFFIX = '.githubusercontent.com'


def is_trusted_url(url: str) -> bool:
    """True only for https URLs on GitHub or its asset hosts."""
    try:
        parts = urllib.parse.urlparse(url or '')
    except ValueError:
        return False
    if parts.scheme != 'https':
        return False
    host = (parts.hostname or '').lower()
    return host in _TRUSTED_HOSTS or host.endswith(_TRUSTED_SUFFIX)


class _TrustedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Abort rather than follow a redirect that leaves GitHub or drops TLS."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not is_trusted_url(newurl):
            raise urllib.error.HTTPError(
                newurl, code, 'refusing a redirect off GitHub', headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(req, timeout: int):
    opener = urllib.request.build_opener(
        _TrustedRedirectHandler,
        urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    return opener.open(req, timeout=timeout)


# ---------------------------------------------------------------------------
# Version comparison (no `packaging` dependency)
# ---------------------------------------------------------------------------

def parse_version(v: str) -> tuple:
    """'v2.0.1' / '2.0.1-rc1' -> (2, 0, 1).

    Drops a leading 'v', keeps the leading digits of each dot-separated
    segment so a pre-release suffix is ignored, and never raises.
    """
    if not v:
        return (0,)
    v = v.strip()
    if v[:1] in ('v', 'V'):
        v = v[1:]
    parts = []
    for seg in v.split('.'):
        digits = ''
        for ch in seg.strip():
            if not ch.isdigit():
                break
            digits += ch
        parts.append(int(digits) if digits else 0)
    return tuple(parts) if parts else (0,)


def is_newer(remote: str, local: str) -> bool:
    """True iff remote is strictly higher, zero-padding the shorter version."""
    r, l = parse_version(remote), parse_version(local)
    n = max(len(r), len(l))
    return r + (0,) * (n - len(r)) > l + (0,) * (n - len(l))


# ---------------------------------------------------------------------------
# GitHub release fetch
# ---------------------------------------------------------------------------

def normalize_release(data: dict) -> Optional[dict]:
    """Reduce a GitHub releases/latest payload to what the UI needs.

    Pure, so tests can feed a recorded payload. Returns None when the payload
    carries no distributable zip.
    """
    if not isinstance(data, dict):
        return None
    tag = data.get('tag_name') or ''
    asset = next((a for a in (data.get('assets') or [])
                  if isinstance(a, dict) and ASSET_PATTERN.match(a.get('name') or '')), None)
    if asset is None or not is_trusted_url(asset.get('browser_download_url')):
        return None
    sha = None
    digest = asset.get('digest')
    if isinstance(digest, str) and digest.lower().startswith('sha256:'):
        sha = digest.split(':', 1)[1].strip().lower() or None
    return {
        'tag': tag,
        'version': tag[1:] if tag[:1] in ('v', 'V') else tag,
        'asset_name': asset.get('name'),
        'asset_url': asset['browser_download_url'],
        'asset_size': int(asset.get('size') or 0),
        'sha256': sha,
        'html_url': data.get('html_url') or '',
        'notes': data.get('body') or '',
        'published_at': data.get('published_at') or '',
    }


def fetch_latest_release(owner: str = GITHUB_OWNER, repo: str = GITHUB_REPO,
                         timeout: int = 10) -> Optional[dict]:
    """GET the latest release, or None on any failure.

    Callers turn None into "could not reach GitHub" rather than "up to date":
    a network error must never be reported as being on the newest version.
    """
    url = f'https://api.github.com/repos/{owner}/{repo}/releases/latest'
    req = urllib.request.Request(url, headers={
        'User-Agent': _USER_AGENT,
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
    })
    try:
        with _open(req, timeout) as resp:
            data = json.loads(resp.read().decode('utf-8'))
    except Exception:
        return None
    return normalize_release(data)


# ---------------------------------------------------------------------------
# Download + integrity
# ---------------------------------------------------------------------------

def download_asset(url: str, dest_path: str,
                   progress_cb: Optional[Callable[[int, int], None]] = None,
                   chunk: int = 1 << 16, timeout: int = 30,
                   total_hint: int = 0) -> str:
    """Stream url to dest_path via a .part file so a killed download never
    leaves a half-written archive that looks complete."""
    if not is_trusted_url(url):
        raise ValueError(f'refusing to download from an untrusted URL: {url}')
    part = dest_path + '.part'
    req = urllib.request.Request(url, headers={'User-Agent': _USER_AGENT})
    try:
        with _open(req, timeout) as resp:
            total = int(resp.headers.get('Content-Length') or 0) or total_hint
            done = 0
            with open(part, 'wb') as fh:
                while True:
                    buf = resp.read(chunk)
                    if not buf:
                        break
                    fh.write(buf)
                    done += len(buf)
                    if progress_cb:
                        progress_cb(done, total)
        os.replace(part, dest_path)
    except BaseException:
        try:
            os.remove(part)
        except OSError:
            pass
        raise
    return dest_path


def verify_sha256(path: str, expected: Optional[str]) -> bool:
    """True when expected is absent (GitHub's digest is best-effort) or matches."""
    if not expected:
        return True
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for block in iter(lambda: fh.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest().lower() == expected.strip().lower()


def extract_payload(zip_path: str, dest_dir: str) -> list:
    """Unpack the release zip, refusing entries that escape dest_dir.

    A zip is attacker-controllable input in principle, so paths are checked
    rather than trusted; the archive is flat by construction.
    """
    os.makedirs(dest_dir, exist_ok=True)
    written = []
    dest_root = os.path.abspath(dest_dir)
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            if name.endswith('/'):
                continue
            target = os.path.abspath(os.path.join(dest_root, name))
            if os.path.commonpath([dest_root, target]) != dest_root:
                raise ValueError(f'archive entry escapes the target directory: {name}')
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zf.open(name) as src, open(target, 'wb') as out:
                out.write(src.read())
            written.append(os.path.basename(target))
    if EXE_NAME not in written:
        raise ValueError(f'release archive has no {EXE_NAME}')
    return written


# ---------------------------------------------------------------------------
# Frozen-build helpers + Windows self-replace
# ---------------------------------------------------------------------------

def is_frozen() -> bool:
    """True only in the PyInstaller onefile build."""
    return bool(getattr(sys, 'frozen', False))


def current_exe_path() -> str:
    return os.path.abspath(sys.executable)


# CREATE_NO_WINDOW gives the helper its own console and keeps it hidden.
# DETACHED_PROCESS would leave it with no console at all, and `start` cannot
# then allocate one for the console-mode exe it relaunches - the swap still
# succeeds but the app never comes back, which is exactly what happened when
# this was first tested against a real release.
_CREATE_NO_WINDOW = 0x08000000


HEALTH_URL = 'http://127.0.0.1:5000/api/version'


def ps_literal(s: str) -> str:
    """Quote a string as a PowerShell single-quoted literal.

    Windows lets a directory be named `$(whatever)`, and inside a double-quoted
    PowerShell string that is a subexpression evaluated before the cmdlet ever
    runs - so interpolating an install path into one hands whoever named the
    directory a command-execution primitive, under -ExecutionPolicy Bypass no
    less. Single quotes suppress every form of expansion; doubling is the only
    escape they need.
    """
    return "'" + str(s).replace("'", "''") + "'"


def build_swap_script(staged_dir: str, target_dir: str, exe_name: str = EXE_NAME,
                      relaunch: bool = True, health_url: str = HEALTH_URL) -> str:
    """PowerShell helper that swaps the install and brings the app back.

    A cmd script could move the files but could not tell whether the relaunched
    process actually came up - and in practice it often did not, leaving the
    user on a dead page having been told the window would reopen. PowerShell
    can start the process and then poll the health endpoint, so the helper
    retries instead of assuming.

    The relaunched instance gets CMIS_NO_BROWSER=1: the user is already looking
    at the page, which reconnects on its own, and opening another tab on every
    update is how tabs pile up.

    Every wait is bounded. A permanent failure - antivirus quarantine, an
    unwritable directory, an app that never exits - must not leave a hidden
    PowerShell spinning forever; giving up leaves the working old version in
    place, which is the safe outcome.
    """
    # Every path reaches PowerShell as a single-quoted literal: see ps_literal.
    q_staged_dir = ps_literal(staged_dir)
    q_target_dir = ps_literal(target_dir)
    q_staged_exe = ps_literal(os.path.join(staged_dir, exe_name))
    q_target_exe = ps_literal(os.path.join(target_dir, exe_name))
    q_log = ps_literal(os.path.join(target_dir, 'update.log'))
    q_health = ps_literal(health_url)
    # Start-Process goes through ShellExecute, which needs a usable window
    # station; spawned from an exiting console app it silently created nothing
    # while reporting no error. Going straight to CreateProcess avoids that.
    relaunch_block = f'''
for ($attempt = 1; $attempt -le 2; $attempt++) {{
    Log "relaunch attempt $attempt"
    try {{
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = {q_target_exe}
        $psi.WorkingDirectory = {q_target_dir}
        $psi.UseShellExecute = $false
        # The helper inherited the dying app's environment, which carries
        # PyInstaller's bootloader handshake variables. Handing those to a
        # fresh onefile build is meaningless at best, so start it clean. This
        # is hygiene, not a proven fix for the relaunch problem below.
        @($psi.EnvironmentVariables.Keys) | Where-Object {{
            $_ -like "_MEI*" -or $_ -like "_PYI*"
        }} | ForEach-Object {{ $psi.EnvironmentVariables.Remove($_) }}
        $psi.EnvironmentVariables["CMIS_NO_BROWSER"] = "1"
        $proc = [System.Diagnostics.Process]::Start($psi)
        Log ("started pid " + $proc.Id)
    }} catch {{
        Log ("start failed: " + $_.Exception.Message)
        Start-Sleep -Seconds 2
        continue
    }}
    for ($w = 0; $w -lt 15; $w++) {{
        Start-Sleep -Seconds 1
        try {{
            Invoke-WebRequest -Uri {q_health} -UseBasicParsing -TimeoutSec 2 | Out-Null
            Log "new build is serving; update complete"
            exit 0
        }} catch {{ }}
    }}
    Log "no response after 15s"
}}
Log "the files are updated; the user starts the exe again from here"
''' if relaunch else ''
    return f'''$ErrorActionPreference = "SilentlyContinue"
$env:CMIS_NO_BROWSER = "1"

# A failed update is otherwise invisible: the app is gone and nothing says why.
function Log($m) {{
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $m" |
        Out-File -FilePath {q_log} -Append -Encoding utf8
}}
Log "update helper started"

# Wait for the old build to release its executable, then take its place.
$swapped = $false
for ($i = 0; $i -lt 150; $i++) {{
    Move-Item -LiteralPath {q_staged_exe} -Destination {q_target_exe} -Force
    if ($?) {{ $swapped = $true; break }}
    Start-Sleep -Milliseconds 500
}}
if (-not $swapped) {{ Log "could not replace the executable; nothing changed"; exit 1 }}
Log "executable replaced"

# The executable is the only file the running app held open.
Get-ChildItem -LiteralPath {q_staged_dir} -File | ForEach-Object {{
    Move-Item -LiteralPath $_.FullName -Destination {q_target_dir} -Force
}}
Remove-Item -LiteralPath {q_staged_dir} -Recurse -Force
Log "supporting files replaced"
{relaunch_block}exit 0
'''


def stage_and_swap(staged_dir: str, target_dir: Optional[str] = None,
                   relaunch: bool = True) -> subprocess.Popen:
    """Launch the helper that replaces this install and brings the app back.

    Frozen-only. The caller must exit promptly afterwards so the exe's file
    lock drops and the helper's move can succeed.

    -ExecutionPolicy Bypass applies to this invocation only; without it a
    machine whose policy forbids scripts would swap nothing.
    """
    if not is_frozen():
        raise RuntimeError('stage_and_swap() is only valid in a frozen build')
    target = os.path.abspath(target_dir or os.path.dirname(current_exe_path()))
    script = os.path.join(tempfile.gettempdir(), f'cmis_update_{os.getpid()}.ps1')
    with open(script, 'w', encoding='utf-8') as fh:
        fh.write(build_swap_script(os.path.abspath(staged_dir), target,
                                   relaunch=relaunch))
    return subprocess.Popen(
        ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
         '-WindowStyle', 'Hidden', '-File', script],
        creationflags=_CREATE_NO_WINDOW,
        close_fds=True, cwd=target,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def staging_dir() -> str:
    """Where the update is unpacked: beside the exe, so the swap move is a
    rename on the same volume rather than a cross-device copy."""
    return os.path.join(os.path.dirname(current_exe_path()), '_cmis_update')
