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


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit(__doc__.strip().split('\n\n')[1])
    out_dir = sys.argv[1]
    ver = version()
    dest = os.path.join(out_dir, 'CMIS_dist_v%s.zip' % ver.replace('.', '_'))

    entries = members()
    assert_updater_accepts([n for _p, n in entries])

    with zipfile.ZipFile(dest, 'w', zipfile.ZIP_DEFLATED) as z:
        for src, name in entries:
            z.write(src, name)

    with zipfile.ZipFile(dest) as z:
        packed = z.namelist()
    assert_updater_accepts(packed)

    print('%s  (v%s)' % (dest, ver))
    for n in packed:
        print('  %10d  %s' % (os.path.getsize(dict((b, a) for a, b in entries)[n]), n))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
