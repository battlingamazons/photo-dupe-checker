# Photo Duplicate Checker

Asks "have we already scanned this photo?" by comparing how photos *look*, so it
still works when the new scan has a different size, brightness, file type or
orientation. Everything runs offline on the local computer; no photos leave it.

## Getting the app
1. On GitHub open the repo's **Actions** tab, click the latest green run.
2. Download **PhotoDupeChecker-windows** from *Artifacts* and unzip it.
3. Copy `PhotoDupeChecker.exe` anywhere (e.g. the desktop) and double-click it.
   Windows may say "Windows protected your PC" because the app is unsigned:
   click **More info**, then **Run anyway**.

## Using it
1. **Add folder...** for each folder of already-scanned photos. The first scan of
   tens of thousands of photos takes a while (progress bar shown); later
   **Rescan folders** only processes new or changed files.
2. **Choose a scan to check...** and pick the new scan's file.
3. Read the verdict and compare side by side. Matches are suggestions; always
   confirm by eye. Sensitivity "Generous" shows more maybes.
4. If it really is new, **Add this photo to the collection** (or just save it
   into a watched folder and rescan).

The index is stored in `%APPDATA%\PhotoDupeChecker\index.db`. Photo files are
never modified. If a folder's drive is unplugged during a rescan, its entries
are kept, not deleted.

## Limits
Heavily cropped or very differently toned scans can score lower, and two prints
from the same negative look nearly identical.

## File formats
TIFF is the priority and is tested extensively: 8/16-bit and 48-bit colour,
grayscale, CMYK, LZW/ZIP/PackBits/JPEG compression, tiled, planar, BigTIFF,
big-endian, and multi-page (first page is used). Also: JPEG, PNG, BMP, GIF, WebP,
JPEG 2000, TGA, PCX, PPM/PGM, PSD, ICO, AVIF, HEIC/HEIF, PDF (first page) and
camera RAW (DNG, CR2/CR3, NEF, ARW, ORF, RW2, RAF, PEF and more).

## Developing
`pip install -r requirements.txt pytest && python -m pytest`
The Windows build is defined in `.github/workflows/build.yml`.
