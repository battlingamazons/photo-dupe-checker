# Photo Duplicate Checker

Asks "have we already scanned this photo?" by comparing how photos *look*, so it
still works when the new scan has a different size, brightness, file type or
orientation. Everything runs offline on the local computer; no photos leave it.

## Installing
1. On GitHub open the repo's **Releases** page (right-hand side of the repo home page).
2. Download the `.msi` under the newest release and double-click it. It installs like
   any other program and adds Start Menu and Desktop shortcuts.
   Windows may say "Windows protected your PC" because the installer is unsigned:
   click **More info**, then **Run anyway**.
3. To remove it: Settings > Apps > Photo Duplicate Checker > Uninstall. Your photos
   and the index file are not touched.

Installing a newer version over an older one upgrades it in place.

## Making a new release (developers)
`git tag v1.0.1 && git push origin v1.0.1` - GitHub builds, tests (including a real
install/run/uninstall) and publishes the MSI on the Releases page. Every push to
`main` also builds one, available under Actions > the run > Artifacts.

## Using it
1. **Add folder...** for each folder of already-scanned photos. The first scan of
   tens of thousands of photos takes a while (progress bar shown); later
   **Rescan folders** only processes new or changed files.
2. **Choose a scan to check...** and pick the new scan's file.
3. Read the verdict and compare side by side. Matches are suggestions; always
   confirm by eye. Sensitivity "Generous" shows more maybes.
3b. Blank paper or scanner background around the photo is cropped away automatically (the result is
   labelled "auto-cropped"; the whole scan is also tried, so nothing is lost). Click **Crop...** to
   see the box, drag its edges or draw a new one, and **Check this part** to re-run the search on it.
3c. If a byte-for-byte identical file (same SHA-256 hash) is already in the collection, the verdict says
   "Exact copy already in the collection" and that file is shown first as **EXACT MATCH**. Photos indexed by an
   older version get their hash filled in by the next **Rescan folders** (it only reads the files, no re-analysis).
   A re-scan or re-saved copy is never byte-identical, so those are still found by the look-alike matching.
4. If it really is new, **Add this photo to the collection** (or just save it
   into a watched folder and rescan).

The index is stored in `%APPDATA%\PhotoDupeChecker\index.db`. Photo files are
never modified. If a folder's drive is unplugged during a rescan, its entries
are kept, not deleted.

## How matching works
Each photo is reduced to a small structural fingerprint that ignores overall brightness,
fading, yellowing and stains. A new scan is compared to every stored photo at many slight
zoom / shift / rotation positions, so these still match the original copy:
faded, yellowed or stained prints; dust and scratches; torn corners; creases; scanner
flat borders and dark scanner-bed edges (these are cut away before comparing, so two unrelated scans that share the same border are not mistaken for each other); crops; a few degrees of skew; a sideways scan;
lower resolution; and different file formats (e.g. a JPEG vs. an older TIFF).

The four closest photos are always shown, even when the verdict is "looks new", so a
badly worn copy can still be recognised by eye. Matches are suggestions; confirm by eye.

## Limits
Very heavily damaged copies (faint, stained, creased, cropped and bordered all at once)
may score low, but usually still appear among the four closest. Photos with large
featureless areas, or two prints from the same negative, can look alike. Different photos
of the same scene or pose can score as "possible match".

When an update changes how photos are matched, the index is rebuilt automatically the
first time the new version starts (the progress bar shows it).

## File formats
TIFF is the priority and is tested extensively: 8/16-bit and 48-bit colour,
grayscale, CMYK, LZW/ZIP/PackBits/JPEG compression, tiled, planar, BigTIFF,
big-endian, and multi-page (first page is used). Also: JPEG, PNG, BMP, GIF, WebP,
JPEG 2000, TGA, PCX, PPM/PGM, PSD, ICO, AVIF, HEIC/HEIF, PDF (first page) and
camera RAW (DNG, CR2/CR3, NEF, ARW, ORF, RW2, RAF, PEF and more).

## License
The program's own code is MIT licensed (`LICENSE`). It is built on open-source libraries, some under the
GPL/LGPL (HEIC and RAW support); they are acknowledged in `THIRD_PARTY_NOTICES.md`, and their full license
texts are installed in the `licenses` folder next to the program (also reachable from the **About & licenses**
button). Because the installer bundles GPL-2.0 code, see that file for what it means and how to build a
GPL-free installer.

## Developing
`pip install -r requirements.txt pytest && python -m pytest`
The Windows build is defined in `.github/workflows/build.yml`; the installer in `Package.wxs`.
