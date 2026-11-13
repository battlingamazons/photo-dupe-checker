# Third-party notices

Photo Duplicate Checker's own source code is MIT licensed (see `LICENSE`). The program is built
from open-source components, and the Windows installer ships them. They keep their own licenses,
which are acknowledged here. The full license texts of everything shipped are in the `licenses`
folder next to the program (installed at `C:\Program Files\Photo Duplicate Checker\licenses`).

## Important: the Windows installer contains GPL / LGPL components

HEIC/HEIF photo support comes from the `pillow-heif` package. Its Windows build bundles:

| Component | License | Source |
|---|---|---|
| libheif | LGPL-3.0 | https://github.com/strukturag/libheif |
| libde265 | LGPL-3.0 | https://github.com/strukturag/libde265 |
| x265 | **GPL-2.0** | https://github.com/Multicorewareinc/x265 |
| MinGW-w64 runtime (libgcc, libstdc++, libwinpthread) | GPL-3.0 with GCC Runtime Library Exception; MIT/BSD | https://www.mingw-w64.org |

The exact versions are listed in `licenses\pillow_heif\LICENSES_bundled.txt`, and the matching
source code is available from the links above (and from the `pillow-heif` project,
https://github.com/bigcat88/pillow_heif, which builds these wheels).

What this means in practice:
* The libraries are shipped as separate `.dll` files in the install folder, so they can be
  replaced with other builds of the same libraries (this is what the LGPL requires).
* Because the installer combines our code with GPL-2.0 code (x265), the installer **as a whole**
  is distributed under the terms of the GPL-2.0. The complete source of this program is public
  at https://github.com/battlingamazons/photo-dupe-checker, and the licenses are in `licenses`.
  The program's own source code remains MIT licensed.
* If you would rather ship an installer with no GPL code, remove HEIC support: delete
  `pillow-heif` from `requirements.txt` and the `--collect-all pillow_heif` build option.

LibRaw (camera RAW support, via `rawpy`) is licensed LGPL-2.1 or CDDL-1.0; it is used here under
the LGPL-2.1 (license text in `licenses\rawpy\LICENSE.LibRaw`; source: https://github.com/LibRaw/LibRaw).

## Python packages (permissive licenses)

| Package | License | Project |
|---|---|---|
| Pillow | MIT-CMU (HPND) | https://python-pillow.org |
| NumPy | BSD-3-Clause (plus bundled BSD/MIT/Zlib/CC0 parts) | https://numpy.org |
| tifffile | BSD-3-Clause | https://github.com/cgohlke/tifffile |
| imagecodecs | BSD-3-Clause (plus the licenses of the bundled codec libraries, in `licenses\imagecodecs`) | https://github.com/cgohlke/imagecodecs |
| pillow-heif | BSD-3-Clause (the bundled binaries are listed above) | https://github.com/bigcat88/pillow_heif |
| pypdfium2 | BSD-3-Clause or Apache-2.0, with PDFium (BSD-3-Clause / Apache-2.0) and its dependencies | https://github.com/pypdfium2-team/pypdfium2 |
| rawpy | MIT, bundling LibRaw (above) | https://github.com/letmaik/rawpy |
| tkinterdnd2 | MIT, bundling tkdnd | https://github.com/Eliav2/tkinterdnd2 |

## Runtime and build tools

| Component | License | Project |
|---|---|---|
| Python | PSF License | https://www.python.org |
| Tcl/Tk (used by Tkinter) | Tcl/Tk (BSD-style) license | https://www.tcl-lang.org |
| PyInstaller (packages the program) | GPL-2.0 with a special exception that allows the bundled program to use any license | https://pyinstaller.org |
| WiX Toolset (builds the .msi; not shipped) | MS-RL | https://wixtoolset.org |

The program icon was created for this project.

## Reporting a problem

If you believe a component is missing from this list or mis-described, please open an issue on the
GitHub repository.
