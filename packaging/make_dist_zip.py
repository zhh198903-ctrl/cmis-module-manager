"""Build the customer distribution zip.

Run after packaging/build_exe.bat, with the output directory as the argument:

    python packaging/make_dist_zip.py <output-dir>

The payload MUST be flat - CMIS_Module_Manager.exe directly at the archive
root. The self-updater stages the zip and looks for the exe in the staging
root with a non-recursive listing; v2.1.0 and v2.2.0 shipped the containing
folder instead and the swap silently did nothing for every user until they
noticed the version never changed. Existing installs carry their own copy of
that logic frozen inside their exe, so a nested payload cannot be rescued by
fixing the updater afterwards.

That is also why the Claude Code skill ships as `SKILL.md` at the root rather
than under `skill/`: updater._payload_members rejects any nested entry.
"""

import os
import re
import struct
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAYLOAD_DIR = os.path.join(ROOT, 'CMIS2Customer')
SKILL = os.path.join(ROOT, 'skill', 'SKILL.md')


def version() -> str:
    """Read it from app.py so the file name cannot drift from the build."""
    with open(os.path.join(ROOT, 'app.py'), encoding='utf-8') as f:
        m = re.search(r"^__version__\s*=\s*'([^']+)'", f.read(), re.M)
    if not m:
        raise SystemExit('app.py has no __version__')
    return m.group(1)


def members() -> list:
    """(source path, name inside the archive) for everything shipped."""
    out = [(os.path.join(PAYLOAD_DIR, n), n)
           for n in sorted(os.listdir(PAYLOAD_DIR))
           if os.path.isfile(os.path.join(PAYLOAD_DIR, n))]
    if not any(n.lower().endswith('.exe') for _p, n in out):
        raise SystemExit('no exe in %s - run packaging/build_exe.bat first'
                         % PAYLOAD_DIR)
    # The companion skill, flat. Also published on its own as
    # CMIS-Skill_dist_v<ver>.zip for people who already have the tool.
    out.append((SKILL, 'SKILL.md'))
    return out


def assert_updater_accepts(names: list) -> None:
    """Refuse to ship a payload our own updater would reject.

    Checking it here rather than trusting the layout: the two releases this
    broke looked perfectly reasonable in a file listing.
    """
    sys.path.insert(0, ROOT)
    import updater
    staged = [rel for _orig, rel in updater._payload_members(names)]
    exes = [n for n in staged if n.lower().endswith('.exe')]
    if len(exes) != 1:
        raise SystemExit('staging root must hold exactly one exe, got %s' % exes)


IMAGE_FILE_MACHINE_I386 = 0x014C


def pe_machine(path: str) -> int:
    """The Machine field of a PE image, read from the file itself."""
    with open(path, 'rb') as f:
        head = f.read(0x400)
    if head[:2] != b'MZ':
        raise SystemExit('%s is not a PE image' % path)
    off = struct.unpack_from('<I', head, 0x3C)[0]
    if head[off:off + 4] != b'PE\0\0':
        raise SystemExit('%s has no PE header' % path)
    return struct.unpack_from('<H', head, off + 4)[0]


def assert_exe_is_32bit(path: str) -> None:
    """Refuse to ship an exe that cannot talk to the primary adapter.

    The WCH driver installs a 32-bit CH341DLL.dll and a 64-bit process cannot
    load it, so a 64-bit build starts, serves the interface, lists every mock,
    and cannot drive the adapter this tool exists to drive.

    The backend says so plainly once someone with a CH341 opens the list -
    "found ... but failed to load. This EXE is 64-bit" - which is the point:
    the only thing that can tell the two artifacts apart is a user with the
    hardware, after release. The file listing, the size, the version banner,
    the startup log and every test in the suite are identical either way,
    because the tests all run on mock backends and a mock does not care how
    wide the process is.

    build_exe.bat picks the interpreter, and it used to fall back to whatever
    "python" was on PATH. That is the right default for a local test build and
    the wrong one for a release, and the release is the one nobody can tell
    apart afterwards. So the check lives here, on the artifact, rather than on
    the build that produced it.
    """
    machine = pe_machine(path)
    if machine != IMAGE_FILE_MACHINE_I386:
        raise SystemExit(
            'refusing to package a 0x%04X exe: the shipped build must be '
            '32-bit (0x%04X) or it cannot load the 32-bit CH341DLL.dll. '
            'Rebuild with a 32-bit interpreter: set CMIS_PYTHON=<path to a '
            '32-bit python.exe> (or install one for the py -3-32 launcher) '
            'and run packaging/build_exe.bat again.'
            % (machine, IMAGE_FILE_MACHINE_I386))


def write_skill_zip(out_dir: str, ver: str) -> str:
    """The companion skill, published on its own as well as bundled.

    Same version as the program by construction: the download site merges it
    into the product card by name and warns when the two drift, because a
    skill describing a build the user does not have sends them looking for
    behaviour that is not there.
    """
    dest = os.path.join(out_dir,
                        'CMIS-Skill_dist_v%s.zip' % ver.replace('.', '_'))
    root, sub = 'CMIS-Skill_dist', 'cmis-module-manager'
    with zipfile.ZipFile(dest, 'w', zipfile.ZIP_DEFLATED) as z:
        z.write(SKILL, '%s/%s/SKILL.md' % (root, sub))
        z.write(os.path.join(ROOT, 'skill', 'INSTALL.md'),
                '%s/%s/INSTALL.md' % (root, sub))
        z.write(os.path.join(ROOT, 'LICENSE'), '%s/LICENSE' % root)
    return dest



def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit(__doc__.strip().split('\n\n')[1])
    out_dir = sys.argv[1]
    ver = version()
    dest = os.path.join(out_dir, 'CMIS_dist_v%s.zip' % ver.replace('.', '_'))

    entries = members()
    assert_updater_accepts([n for _p, n in entries])
    for src, name in entries:
        if name.lower().endswith('.exe'):
            assert_exe_is_32bit(src)

    with zipfile.ZipFile(dest, 'w', zipfile.ZIP_DEFLATED) as z:
        for src, name in entries:
            z.write(src, name)

    with zipfile.ZipFile(dest) as z:
        packed = z.namelist()
    assert_updater_accepts(packed)

    print('%s  (v%s)' % (dest, ver))
    for n in packed:
        print('  %10d  %s' % (os.path.getsize(dict((b, a) for a, b in entries)[n]), n))

    skill_zip = write_skill_zip(out_dir, ver)
    print(skill_zip)
    with zipfile.ZipFile(skill_zip) as z:
        for n in z.namelist():
            print('  %10d  %s' % (z.getinfo(n).file_size, n))

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
