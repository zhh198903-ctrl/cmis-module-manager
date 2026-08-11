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
import time
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

# The personal download site, which keeps a copy of every release asset at
# /<product>/<version>/<file>. Which of the two is faster changes by the hour
# here - GitHub's object store has answered at 15 KB/s and at nothing at all -
# so both are offered and the quicker one wins.
#
# It is a byte mirror and nothing more. It publishes no version list and no
# digest, so what the newest release is and what it must hash to still come
# from GitHub over TLS. That is also why plain http is acceptable for it: the
# worst a tampered mirror can do is fail the SHA-256 check and abort the
# update, which is the same outcome as the download simply not working.
MIRROR_BASE = 'http://106.14.76.130'
MIRROR_PRODUCT = 'CMIS'
_MIRROR_HOST = '106.14.76.130'


def is_trusted_url(url: str) -> bool:
    """True only for https URLs on GitHub or its asset hosts.

    This gates what may *tell us about* a release - the API reply and the asset
    URL inside it. Where the bytes may be fetched from is a weaker question,
    answered by is_allowed_source().
    """
    try:
        parts = urllib.parse.urlparse(url or '')
    except ValueError:
        return False
    if parts.scheme != 'https':
        return False
    host = (parts.hostname or '').lower()
    return host in _TRUSTED_HOSTS or host.endswith(_TRUSTED_SUFFIX)


def is_allowed_source(url: str) -> bool:
    """True for hosts the payload bytes may be pulled from.

    Wider than is_trusted_url by exactly one pinned host, and only because the
    digest that decides whether the result is installable comes from elsewhere.
    """
    if is_trusted_url(url):
        return True
    try:
        parts = urllib.parse.urlparse(url or '')
    except ValueError:
        return False
    return parts.scheme == 'http' and (parts.hostname or '').lower() == _MIRROR_HOST


def mirror_url(version: str, asset_name: str) -> str:
    """Where the download site keeps one release asset."""
    return f'{MIRROR_BASE}/{MIRROR_PRODUCT}/{version}/{asset_name}'


class _TrustedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Abort rather than follow a redirect that leaves GitHub or drops TLS."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not is_allowed_source(newurl):
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
                         timeout: int = 10, attempts: int = 3,
                         retry_wait: float = 2.0) -> Optional[dict]:
    """GET the latest release, or None once the retries are spent.

    Callers turn None into "could not reach GitHub" rather than "up to date":
    a network error must never be reported as being on the newest version.
    That makes a single dropped connection look like the update server is
    unreachable, which on a proxied link happens often enough to be worth
    retrying before telling the user so.
    """
    url = f'https://api.github.com/repos/{owner}/{repo}/releases/latest'
    req = urllib.request.Request(url, headers={
        'User-Agent': _USER_AGENT,
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
    })
    for attempt in range(attempts):
        try:
            with _open(req, timeout) as resp:
                data = json.loads(resp.read().decode('utf-8'))
            return normalize_release(data)
        except Exception:
            if attempt + 1 < attempts:
                time.sleep(retry_wait)
    return None


# ---------------------------------------------------------------------------
# Download + integrity
# ---------------------------------------------------------------------------

def probe_source(url: str, seconds: float = 4.0, timeout: int = 15,
                 offset: int = 0) -> float:
    """Bytes per second measured over a short read, or 0.0 if unusable.

    Timed rather than sized: a fixed 256 KB probe is instant on a good link and
    takes half an hour on the bad one this is meant to detect, which would cost
    more than picking wrong. Reading for a fixed few seconds costs the same
    either way and still separates the two by orders of magnitude.

    A source that 404s, refuses Range or cannot be reached scores 0.0 rather
    than raising - not being able to measure it is a reason to prefer the other
    one, not a reason to fail.

    The clock starts only once the response headers are in. Timing the connect
    as well made both sources score zero on a link where the handshake alone
    outlasted the window, which turned the whole measurement into "keep the
    order they were passed in". Connect cost is paid once; throughput is what
    decides sixteen megabytes.
    """
    if not is_allowed_source(url):
        return 0.0
    req = urllib.request.Request(url, headers={
        'User-Agent': _USER_AGENT, 'Range': f'bytes={offset}-'})
    got = 0
    try:
        with _open(req, timeout) as resp:
            started = time.monotonic()
            while time.monotonic() - started < seconds:
                buf = resp.read(1 << 15)
                if not buf:
                    break
                got += len(buf)
            elapsed = time.monotonic() - started
    except Exception:
        return 0.0
    return got / elapsed if elapsed > 0 and got else 0.0


def order_sources(urls: list, seconds: float = 4.0, timeout: int = 15,
                  offset: int = 0) -> list:
    """[(url, bytes_per_second)] fastest first, measured right now.

    Unreachable sources sort last but are kept: when every probe scores zero
    the download should still be attempted rather than refused on the strength
    of a four-second sample.

    The connect timeout is deliberately generous. A handshake here has taken
    ten seconds through a proxy, and a probe that gives up before that scores
    a perfectly usable source as dead - which is worse than not probing, since
    it looks like a measurement.
    """
    scored = [(probe_source(u, seconds, timeout, offset), i, u)
              for i, u in enumerate(urls)]
    scored.sort(key=lambda s: (-s[0], s[1]))
    return [(u, rate) for rate, _, u in scored]


def download_asset(url, dest_path: str,
                   progress_cb: Optional[Callable[[int, int], None]] = None,
                   chunk: int = 1 << 16, timeout: int = 30,
                   total_hint: int = 0, attempts: int = 6,
                   retry_wait: float = 2.0, part_path: Optional[str] = None,
                   keep_partial: bool = False) -> str:
    """Stream url to dest_path via a .part file, resuming after a dropped link.

    A 16 MB asset over a slow or proxied connection routinely dies mid-transfer
    - the object store just closes the socket, which surfaces as an SSL
    "unexpected EOF" - and starting over from zero can never finish if the link
    drops more often than a full download takes. Each retry asks for only the
    bytes still missing and appends to the same .part file, so progress
    survives; the file is renamed into place only once it is complete.

    A server that ignores Range answers 200 instead of 206, and then the file
    has to start over rather than be corrupted by appended duplicates. Either
    way the caller's SHA-256 check is what makes the result safe to trust - a
    resume that silently stitched together the wrong bytes fails it.

    part_path puts the partial file somewhere the caller controls, and
    keep_partial leaves it there when the attempts run out, so a download can
    carry on across separate runs instead of only across retries. The default
    keeps the old behaviour: a partial beside dest_path, removed on failure so
    nothing that looks like the asset is left behind.

    url may be a list of mirrors, fastest first. A failed attempt moves to the
    next one and keeps the same partial file: every mirror serves the identical
    asset, which is what the SHA-256 the caller checks afterwards asserts. So
    the bytes already fetched from a source that has since died are still worth
    keeping, and the switch costs nothing.
    """
    sources = [url] if isinstance(url, str) else list(url)
    if not sources:
        raise ValueError('no download source given')
    for src in sources:
        if not is_allowed_source(src):
            raise ValueError(f'refusing to download from an untrusted URL: {src}')
    part = part_path or dest_path + '.part'
    os.makedirs(os.path.dirname(os.path.abspath(part)), exist_ok=True)
    # The caller's size comes from the release metadata, which is the one
    # statement about this asset that arrived over TLS. A mirror serving a
    # truncated copy must not be able to talk the download into stopping early.
    total = total_hint
    last_err = None
    src_i = 0
    dead = set()
    try:
        for attempt in range(attempts):
            have = os.path.getsize(part) if os.path.exists(part) else 0
            # A leftover longer than the asset cannot be a prefix of it - the
            # release was rebuilt under the same name, or the file is junk.
            if total and have > total:
                open(part, 'wb').close()
                have = 0
            if total and have == total:
                break
            headers = {'User-Agent': _USER_AGENT}
            if have:
                headers['Range'] = f'bytes={have}-'
            req = urllib.request.Request(sources[src_i], headers=headers)
            try:
                with _open(req, timeout) as resp:
                    resumed = have > 0 and resp.getcode() == 206
                    if have and not resumed:
                        have = 0
                    length = int(resp.headers.get('Content-Length') or 0)
                    if not total:
                        total = have + length
                    done = have
                    with open(part, 'ab' if resumed else 'wb') as fh:
                        while True:
                            buf = resp.read(chunk)
                            if not buf:
                                break
                            fh.write(buf)
                            done += len(buf)
                            if progress_cb:
                                progress_cb(done, total)
                if not total or done >= total:
                    break
                last_err = IOError(
                    f'connection closed after {done} of {total} bytes')
            except urllib.error.HTTPError as e:
                # 416 means the .part is already at or past the asset's length,
                # so there is nothing left to ask for and the file is wrong.
                if e.code == 416:
                    open(part, 'wb').close()
                    last_err = e
                elif e.code == 429 or e.code >= 500:
                    last_err = e
                else:
                    # This source will not start working - but the mirror
                    # simply may not carry this release yet, so a 404 on one
                    # of them is not a 404 on all of them.
                    dead.add(sources[src_i])
                    last_err = e
                    if len(dead) >= len(sources):
                        raise
            except OSError as e:
                # ssl.SSLError and socket timeouts land here; these are the
                # ones worth resuming.
                last_err = e
            for _ in range(len(sources)):
                src_i = (src_i + 1) % len(sources)
                if sources[src_i] not in dead:
                    break
            if attempt + 1 < attempts:
                time.sleep(retry_wait)
        else:
            raise last_err or IOError('download did not complete')
        os.replace(part, dest_path)
    except BaseException:
        if not keep_partial:
            try:
                os.remove(part)
            except OSError:
                pass
        raise
    return dest_path


def verify_sha256(path: str, expected: Optional[str]) -> bool:
    """True only when the file matches a digest we were actually given.

    A missing digest is a refusal, not a pass. Installing an unverified build
    is exactly as bad as installing one that failed its check - the difference
    is only whether anyone noticed - and the caller has a safe alternative to
    offer: download it from the release page by hand. Every release asset this
    project has published carries a digest, so this refuses nothing that works
    today; if GitHub ever drops the field the user gets a clear error instead
    of a silent unverified install.
    """
    if not expected:
        return False
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for block in iter(lambda: fh.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest().lower() == expected.strip().lower()


def _payload_members(names: list) -> list:
    """Archive entries paired with the flat name each must be staged under.

    Both zip layouts this project has published must land flat, because the
    swap helper looks for the exe directly in the staging root and moves the
    rest with a non-recursive listing. v2.0.9 and earlier zipped the contents
    of CMIS2Customer/; v2.1.0 and v2.2.0 zipped the folder itself, and nothing
    noticed - the old code staged CMIS2Customer/CMIS_Module_Manager.exe, the
    helper found no exe where it looked, retried for 75 s and gave up without
    ever relaunching. So strip one shared leading directory when every entry
    has it, and refuse anything still nested rather than stall later.
    """
    files = [n for n in names if not n.endswith('/')]
    tops = {n.replace('\\', '/').split('/')[0] for n in files}
    strip = len(tops) == 1 and any('/' in n or '\\' in n for n in files)
    members = []
    for name in files:
        rel = name.replace('\\', '/')
        if strip:
            rel = rel.split('/', 1)[1]
        if '/' in rel or not rel:
            raise ValueError(f'release archive nests {name}; expected a flat payload')
        members.append((name, rel))
    return members


def extract_payload(zip_path: str, dest_dir: str) -> list:
    """Unpack the release zip flat, refusing entries that escape dest_dir.

    A zip is attacker-controllable input in principle, so paths are checked
    rather than trusted.
    """
    os.makedirs(dest_dir, exist_ok=True)
    written = []
    dest_root = os.path.abspath(dest_dir)
    with zipfile.ZipFile(zip_path) as zf:
        for name, rel in _payload_members(zf.namelist()):
            target = os.path.abspath(os.path.join(dest_root, rel))
            if os.path.dirname(target) != dest_root:
                raise ValueError(f'archive entry escapes the target directory: {name}')
            if rel in written:
                raise ValueError(f'release archive stages {rel} twice')
            with zf.open(name) as src, open(target, 'wb') as out:
                out.write(src.read())
            written.append(rel)
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


def download_dir() -> str:
    """Where a partly downloaded asset waits between attempts.

    Deliberately not inside staging_dir(): that one is wiped at the start of
    every update so extraction starts clean, which would throw away the very
    bytes a resume needs. Beside the exe keeps the final rename on one volume.
    """
    return os.path.join(os.path.dirname(current_exe_path()), '_cmis_download')


def partial_path(asset_name: str) -> str:
    """The partial file for one release asset.

    Named after the asset, so a partial left by an abandoned upgrade to one
    version is never mistaken for the start of a different version's download
    - the asset name carries the version.
    """
    return os.path.join(download_dir(), os.path.basename(asset_name) + '.part')


def discard_stale_partials(keep_asset_name: str) -> list:
    """Drop partials for anything but the asset being fetched now.

    Without this a user who gives up on one version leaves 16 MB parked on
    disk for good, and every later release adds another.
    """
    keep = os.path.basename(partial_path(keep_asset_name))
    removed = []
    try:
        names = os.listdir(download_dir())
    except OSError:
        return removed
    for name in names:
        if name == keep:
            continue
        try:
            os.remove(os.path.join(download_dir(), name))
            removed.append(name)
        except OSError:
            pass
    return removed
