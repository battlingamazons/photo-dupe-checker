"""Copy the license texts of everything shipped in the installer into <dest>/licenses.

Run by the build after PyInstaller:  python collect_licenses.py dist/PhotoDupeChecker
"""
import os
import re
import shutil
import sys
from importlib import metadata

ROOT = os.path.dirname(os.path.abspath(__file__))
DIRECT = ["pillow", "numpy", "tifffile", "imagecodecs", "pillow-heif", "pypdfium2", "rawpy", "tkinterdnd2", "pyinstaller"]
PATTERN = re.compile(r"(licen[sc]e|copying|notice|authors)", re.I)


def norm(n):
    return re.sub(r"[-_.]+", "-", n).lower()


def closure(names):
    seen, todo = {}, list(names)
    while todo:
        n = norm(todo.pop())
        if n in seen:
            continue
        try:
            d = metadata.distribution(n)
        except metadata.PackageNotFoundError:
            continue
        seen[n] = d
        for r in d.requires or []:
            if "extra ==" in r:
                continue
            todo.append(re.split(r"[ ;<>=!~(\[]", r, maxsplit=1)[0])
    return seen


def copy_dist_licenses(d, out):
    count = 0
    for f in d.files or []:
        p = str(f).replace("\\", "/")
        in_info = ".dist-info/" in p
        in_pkg_lic = "/licenses/" in p or p.startswith("licenses/")
        if not (PATTERN.search(os.path.basename(p)) or in_pkg_lic):
            continue
        if not (in_info or in_pkg_lic):
            continue
        src = d.locate_file(f)
        if not os.path.isfile(src):
            continue
        rel = p.split("/licenses/", 1)[1] if "/licenses/" in p else os.path.basename(p)
        dst = os.path.join(out, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(src, dst)
        count += 1
    return count


def main(dest):
    lic = os.path.join(dest, "licenses")
    shutil.rmtree(lic, ignore_errors=True)
    os.makedirs(lic)
    for name in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
        shutil.copyfile(os.path.join(ROOT, name), os.path.join(dest, name))
    # plain .txt copies so they open in Notepad
    shutil.copyfile(os.path.join(ROOT, "LICENSE"), os.path.join(dest, "LICENSE.txt"))
    shutil.copyfile(os.path.join(ROOT, "THIRD_PARTY_NOTICES.md"), os.path.join(dest, "THIRD_PARTY_NOTICES.txt"))
    for f in os.listdir(os.path.join(ROOT, "licenses")):
        shutil.copyfile(os.path.join(ROOT, "licenses", f), os.path.join(lic, f))
    total = 0
    for n, d in sorted(closure(DIRECT).items()):
        c = copy_dist_licenses(d, os.path.join(lic, n.replace("-", "_") if n == "pillow-heif" else n))
        total += c
        print(f"{n}: {c} license file(s)")
    # Python itself
    for cand in ("LICENSE.txt", "LICENSE"):
        p = os.path.join(sys.base_prefix, cand)
        if os.path.isfile(p):
            os.makedirs(os.path.join(lic, "python"), exist_ok=True)
            shutil.copyfile(p, os.path.join(lic, "python", "LICENSE.txt"))
            total += 1
            break
    # Tcl/Tk
    for root, _dirs, files in os.walk(sys.base_prefix):
        if "license.terms" in files:
            os.makedirs(os.path.join(lic, "tcl_tk"), exist_ok=True)
            shutil.copyfile(os.path.join(root, "license.terms"), os.path.join(lic, "tcl_tk", "license.terms"))
            total += 1
            break
    print(f"{total} third-party license files copied to {lic}")
    if total < 10:
        sys.exit("Too few license files were found - refusing to build an installer without them")


if __name__ == "__main__":
    main(sys.argv[1])
