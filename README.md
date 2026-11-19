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

**Mac:** on the Releases page download the `.zip` for your Mac (`apple-silicon` = M1 or newer,
`intel` = older Macs), unzip it and open **READ ME FIRST.txt**. The first time, right-click the app,
choose **Open**, then **Open** again (the app is not signed by Apple). The index is kept in
`~/.photodupechecker/index.db`.

Installing a newer version over an older one upgrades it in place.

## Making a new release (developers)
`git tag v1.0.1 && git push origin v1.0.1` - GitHub builds and tests the Windows installer (including a real
install/run/uninstall) and the two Mac apps, then publishes the MSI and the Mac zips on the Releases page. Every push to
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
4. If it really is new, save it into a watched folder (ideally the master folder) and rescan.

The index is stored in `%APPDATA%\PhotoDupeChecker\index.db`. Photo files are
never modified. If a folder's drive is unplugged during a rescan, its entries
are kept, not deleted.

## How matching works
Each photo is reduced to a small structural fingerprint that ignores overall brightness,
fading, yellowing and stains. A new scan is compared to every stored photo at many slight
zoom / shift / rotation positions, so these still match the original copy:
faded, yellowed or stained prints; dust and scratches; torn corners; creases; scanner
flat borders and dark scanner-bed edges (these are cut away before comparing, so two unrelated scans that share the same border are not mistaken for each other); crops; a few degrees of skew; a sideways scan;
lower resolution; one scan cropped tighter than the other (either one may be the tighter; see below); and different file formats (e.g. a JPEG vs. an older TIFF).

The four closest photos are always shown, even when the verdict is "looks new", so a
badly worn copy can still be recognised by eye. Matches are suggestions; confirm by eye.

## Differently cropped copies
Two scans of one photo often differ in how tightly they were cropped - one edge trimmed more than another, a corner cut off.
After the normal comparison, a second pass checks whether one picture is a window cut out of the other: every stored photo
keeps a set of coarse windows (trimmed by up to about a quarter on each side, independently), the new scan is tried whole and
as windows of itself, and the closest candidates are then lined up precisely. Only a precise fit counts, so unrelated photos
are not reported. When the closest match was found this way the verdict line says so. It copes with trims of up to about 25% per side (more than
that may be missed); a match found this way is shown as a "possible match" or "likely duplicate" like any other.
The first time you start a version with this feature, the index is rebuilt once (it needs extra data per photo), which takes
about as long as the first scan did.

## Results and "Show more matches"
The results are a scrolling grid in the main window. It starts with the scan itself, every identical copy, and every match
that is at least 80% similar (and always at least the three closest photos, so there is something to compare). Cropped copies
that count as a match for your Sensitivity setting are always included. **Show more matches** brings the next 12 in below the
ones already shown; the "at least __% similar" box beside it follows along, and typing a number there shows everything down to
that similarity (0 shows everything, up to the best 500). Identical copies are always listed. Only the cards on screen are
built and the pictures load in the background, so hundreds of matches stay responsive.

## Collection audit (master folder vs categories vs the PastPerfect catalogue)
A read-only command-line mode that compares the folder that should hold every photo (the *master* folder), a folder of
category subfolders, and a PastPerfect catalogue, and writes a spreadsheet of problems for a volunteer to work through:

    "Photo Duplicate Checker" --audit --master <master folder> --categories <categories folder> --pp <PastPerfect project folder> --out audit.xlsx

(on a Mac the program is inside the app: `Photo Duplicate Checker.app/Contents/MacOS/Photo Duplicate Checker`). The PastPerfect
Data (the `.DBF` tables), Images and Multimedia folders are found inside `--pp`; if they are not, give them with `--pp-data`,
`--pp-images` and `--pp-multimedia`. The catalogue is read from `PHOTOS.DBF`, `OBJECTS.DBF`, `ARCHIVES.DBF`, `LIBRARY.DBF` and
`MEDIA.DBF`: each record's picture (and high-resolution master) is found the way PastPerfect stores it (relative to the Images
folder, backslashes, trailing dots; masters by file name under Multimedia).

Photos are matched by what they look like (the checker's own matching), so a resaved, resized or renamed copy is still the same
photo; the master folder is treated as the source of truth. The spreadsheet has one sheet per kind of problem, most important first:
catalogue pictures with no match in the master folder, master photos no record uses, the same photo twice in the master folder,
records whose picture file is missing, one photo attached to several records, photos only in the categories folder, and master
photos not filed in a category. Every row has *Status* (a drop-down), *Done by* and *Notes*. Run it again after fixing things: rows
that are still a problem keep their Status and Notes (`--previous old-report.xlsx`), fixed ones disappear. Nothing is ever changed:
the folders and the catalogue are only read, and the audit keeps its own index (`--workdir`, default beside the report) so the
checker's index is untouched (`--reuse-index` can start from a copy of it to save time). The report contains real file and
record names, so keep it on the computer and out of any repository. Limits: the cropped-copy and face passes are not used here,
so a heavily cropped scan shows up as two separate photos; and a very large group of look-alikes is listed for checking by
eye instead of being trusted.

## Extremely cropped copies (found through faces)
When one scan is only a small piece of the other (for example just the people at the bottom of a house photo), the pass above
cannot see it. For photos with faces there is a further check: the program looks for faces in every photo (two small models,
YuNet and SFace, shipped with it; nothing leaves the computer). A face in a crop is the same pixels as in the full photo, so it
matches very closely (typically 0.9 or more, where two different people stay under about 0.5). A close face match tells the
program where in the other photo to look and how big the crop is, and then the actual pixels are compared at that spot. Only if
they agree is it reported ("Same photo, cropped"). Photos with no detectable face are not covered by this check.
The first launch after updating looks for faces in the existing index once (no rebuild; roughly 0.1-0.5 s per photo, so a
collection of tens of thousands takes a while - it can be cancelled and continues next time). New photos are checked as they are added.

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
