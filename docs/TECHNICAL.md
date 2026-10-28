# UHD Blu-ray Toolkit — Technical documentation

> This is the full technical description: configuration, volumes, the pipeline of each tool, the API and local development. For an overview of the app and how to install it, see the [README](../README.md).

> [!NOTE]
> The technical names (the GitHub repository and the Docker image `ghcr.io/aldicarus/hdo-iso-converter:latest`) are kept so that existing deployments do not break. The public name, and the one shown in the UI, is **UHD Blu-ray Toolkit**.

## Four tools in one

The internal panel IDs (`tab-panel-1/2/3/4`) are kept for historical compatibility. The visual order and the UI labels are:

| Pos. | UI label | What it is for |
|---|----------|---|
| 1 | 💿 **Blu-ray ISO → MKV** | Rips a UHD Blu-ray to MKV from three kinds of source (ISO, BDMV folder or M2TS files), keeping Dolby Vision FEL. A mandatory Movie / TV show toggle. TV shows are mapped to episodes through TMDb, with one session per episode. |
| 2 | ✨ **Dolby Vision CMv4.0 upgrade** | A pipeline that injects a CMv4.0 RPU (from DoviTools or other sources) into an MKV that carries the original Blu-ray's CMv2.9, adding the L8-L11 tone-mapping metadata. Quick pre-flight of the bin, a Keep / Inject decision model based on how much colorist work the L8 really carries, frame-by-frame visual sync, and an auto-pipeline that survives the client being closed. |
| 3 | 🔀 **Multiplex MKVs** | What you choose from two MKVs, A and B, in one file: typically the picture of one (the remux, with its Dolby Vision FEL) with the audio, subtitles and chapters of the other (a WEB-DL with the dub the disc lacks), but any mix, section by section. The sync is measured from the sound at seven points of the movie, and when the two run at different frame rates the video's speed is adjusted in the container, with no re-encoding. |
| 4 | 🔬 **Inspect / Edit MKV** | Deep inspection of an MKV (commercial codec names, real bitrate, HDR10, the full Dolby Vision mastering chain, frame-by-frame L1 luminance profile). In-place editing of track names, flags and chapters with no re-encoding (instant, through `mkvpropedit`). |

## Quick start

The official image is published on GHCR as `ghcr.io/aldicarus/hdo-iso-converter:latest` (linux/amd64). There is nothing to compile.

### Option A — Command line

```bash
# 1. Create a working directory
mkdir uhd-bluray-toolkit && cd uhd-bluray-toolkit

# 2. Download docker-compose.yml and the sample .env
curl -LO https://raw.githubusercontent.com/Aldicarus/hdo-iso-converter/main/docker/docker-compose.yml
curl -L https://raw.githubusercontent.com/Aldicarus/hdo-iso-converter/main/docker/.env.example -o .env

# 3. Edit .env with your host's paths (see "Configuration")
nano .env

# 4. Pull the image and start
docker compose pull
docker compose up -d
```

Open **http://localhost:8090** (or `HDO_PORT` if you changed it).

To update to a newer version:

```bash
docker compose pull && docker compose up -d
```

### Option B — Container Station (QNAP) / Portainer

Both are graphical apps for managing Docker containers, and both support `docker-compose.yml`:

1. Download `docker-compose.yml` and `.env.example` from the repository (URLs above).
2. Rename `.env.example` to `.env` and edit the paths.
3. In Container Station / Portainer:
   - Create a new "Docker Compose" application (Container Station: *Applications → Create → Custom* · Portainer: *Stacks → Add stack → Web editor*).
   - Paste the contents of `docker-compose.yml`.
   - Upload the `.env` (Container Station asks for it at the end · Portainer: *Environment variables → Advanced mode → Load variables from .env file*).
   - Create / Deploy.

### Option C — Build from source

Only if you want to modify the code:

```bash
git clone https://github.com/Aldicarus/hdo-iso-converter.git
cd hdo-iso-converter
cp docker/.env.example docker/.env
# Edit docker/.env

cd docker
docker compose up -d --build
```

### Smoke test

```bash
curl http://localhost:8090/api/health
# {"status": "ok"}
```

## Configuration

Variables in `docker/.env`:

| Variable | Required | Default | Description |
|---|---|---|---|
| `HDO_PORT` | no | `8090` | Host port exposed |
| `TZ` | no | `Europe/Madrid` | Container time zone |
| `ISOS_PATH` | **yes** | — | Folder with the source ISOs / BDMV folders / M2TS files (read-only) |
| `LIBRARY_PATH` | no | — | Root of the MKV library (ro). Tabs 2 and 3 browse this tree with the file browser. If it is not set, the tabs only see `/mnt/output` |
| `OUTPUT_PATH` | **yes** | — | Output for the finished MKVs (rw) |
| `TMP_PATH` | **yes** | — | Temporary working directory — an SSD is strongly recommended (rw) |
| `CONFIG_PATH` | **yes** | — | Session JSONs + queue + `app_settings.json` (rw) |
| `CMV40_RPU_PATH` | **yes** | — | External CMv4.0 RPUs, legacy (Tab 3, ro). If you do not use it, point it at an empty directory — never at `/tmp` |
| `TMDB_API_KEY` | no | *(the image ships with the app's key)* | **You do not need to set it.** Only if you want to use your own key without going through the UI. Priority: UI (⚙︎ Settings) > this variable > the app's key |
| `TMDB_APP_KEY` | no | — | Only if you **build from source**. The published image already carries the app's key; a local build comes out without it. It is a *build arg*, not a runtime variable: it is baked in at build time |
| `CMV40_DRIVE_FOLDER_APP` | no | — | The same for the DoviTools repository link. The "Repo" tab **also** needs `GOOGLE_API_KEY`, which is not included |
| `GOOGLE_API_KEY` | no | — | Fallback for the DoviTools repository on Drive. The UI is preferred |

## Docker volumes

| Container path | Type | Description |
|---|---|---|
| `/mnt/isos` | ro | Sources (ISOs / BDMV folders / M2TS files) for Tab 1 |
| `/mnt/library` | ro | MKV library for the file browser of Tabs 2 and 3 |
| `/mnt/output` | rw | Finished MKVs (Tab 1 output, Tabs 2 and 3 input) |
| `/mnt/tmp` | rw | Intermediate MKVs + CMv4.0 workdir (SSD recommended) |
| `/mnt/cmv40_rpus` | ro | External CMv4.0 RPUs, legacy (prefer the online DoviTools repository in Tab 3) |
| `/config` | rw | Sessions + queue + `app_settings.json` with the API keys |

**Space in `/mnt/tmp`:** during the CMv4.0 pipeline, about 2× the size of the source MKV builds up (1.5× on the drop-in route). The pipeline checks it before starting, and the space is freed with "Cleanup" on each project.

---

## The "Blu-ray ISO → MKV" tab

Rips UHD Blu-ray content to MKV with automatic selection of tracks (audio / subtitles / chapters) and Dolby Vision FEL support. "New project" modal → analysis → FIFO queue → execution → validation.

### The "New project" modal

Two mandatory choices before the analysis can start:

**1. Content type** (Movie / TV show toggle):

| | Movie | TV show |
|---|---|---|
| Selection in M2TS | Radio (1 file) | Checkbox (several files) |
| MPLS in ISO/BDMV | The main one is used | MPLS ↔ episode mapping |
| Output | 1 MKV | 1 MKV per episode |

The source selection area is visibly locked (reduced opacity) until the user picks a type — without this, atypical discs (a BDMV with one large m2ts plus extras) were misclassified by auto-detection.

**2. Source type** (3 tabs):

- **💿 ISO** — an `.iso` file (UDF 2.50+). It is mounted with `mount -t udf -o ro,loop` and unmounted at the end.
- **📁 BDMV folder** — a directory with `BDMV/PLAYLIST/` inside (a disc extracted without an ISO). No mount.
- **🎞️ M2TS files** — loose `.m2ts` files. No BDMV → no MPLS → chapters generated automatically every 10 minutes.

All sources live in `/mnt/isos`. Embedded file browser with configurable sorting (by name or by size — useful to spot the large m2ts files among the small extras).

### Duplicate detection

`POST /api/check-duplicate` computes the SHA-256 fingerprint (first 1 MB + size of the main file) and returns **every existing session** with that fingerprint (not just the first one). Behaviour:

- **Movie + 1 previous session** → an "A project already exists" dialog with Open / Re-analyse.
- **TV show + N previous sessions** → straight to the TV show modal with the list of existing ones. Each MPLS/m2ts candidate with a previous session shows a **✓ Exists** badge and starts unchecked. The user checks only what they want to add or redo.

### Multi-episode TV shows

When TV show mode is active and there are several candidates:

1. **Identifying the show** — TMDb search (top 5 candidates with posters), or manual entry if TMDb does not find it.
2. **Choosing the season** — a combo with the episode count of each season.
3. **MPLS ↔ episode mapping** — smart match by offset (tries every starting offset E01/E02/E03/… and picks the one that minimises |Δ runtime| against the TMDb season). It works for discs 2+ of a season, where the episodes are E04-E06 instead of E01-E03.

Confidence indicator per row (recomputed on every change):
- 🟢 **high** — the MPLS runtime matches TMDb within ±1 min.
- 🟡 **low** — the runtime differs, or TMDb has no runtime for that episode.
- ⚪ **no match** — TMDb has no data.

The backend opens the source once, fully analyses each selected MPLS/m2ts and creates N sessions in `pending`. Cost: ~30 s + 15-30 s per episode.

If the user checks an episode that already exists, a dialog offers 3 options:
- **🗑️ Replace** — deletes the existing sessions and creates new ones (losing edits / history).
- **⏭ Skip existing** — only creates the new ones.
- **Cancel**.

**File naming:**

```
/mnt/output/
  Mad Men (2007)/
    Season 01/
      Mad Men (2007) - S01E01 - Smoke Gets in Your Eyes [DV FEL].mkv
      Mad Men (2007) - S01E02 - Ladies Room [DV FEL].mkv
```

Characters that NTFS/SMB forbid (`/\\:*?"<>|`) are sanitised; UTF-8 accents are preserved (ext4/ZFS-safe).

### Automatic selection rules

**Audio:** the app's language and the original version (VO). With the app in Spanish, that is Spanish + VO; in English, English + VO; in Catalan, Catalan + Spanish + VO. For each language, the highest-quality track is picked (TrueHD Atmos > DD+ Atmos > DTS-HD MA > DTS > DD). The first language of the profile is the default track. The `(DCP 9.1.6)` tag goes only on the Spanish TrueHD Atmos track.

**Subtitles:** forced subtitles are detected by packet count and by the Blu-ray block structure. Inclusion order: forced in the preferred language → full in the VO → full in the preferred language → forced in the VO → full in English (if the VO is not English). When there is no audio track in the preferred language, the default subtitle becomes the full one instead of the forced one.

**Chapters:** extracted from the MPLS in Phase A. If there are none (the loose M2TS case), they are generated every 10 minutes. "Generic names" and "Restore from disc" buttons undo edits.

### Analysis pipeline (Phase A)

A pipeline of 4 tools runs with the source open:

1. **mkvmerge -J** — identifies tracks, codecs, languages and structure. It also picks the main title: playlists that play less than half of the largest one are discarded (trailers, extras, menus), and among the rest the one with the most audio tracks wins. If the chosen title plays less than half of the disc's largest m2ts, the log says so.
2. **MediaInfo** — run on the largest m2ts of the chosen playlist. Real bitrate per track, HDR10 metadata (MaxCLL/MaxFALL, mastering display primaries + luminance), definitive Atmos/DTS:X detection through `Format_Commercial`, channel layout.
3. **dovi_tool** — RPU analysis: Profile (4/5/7/8), FEL vs MEL, CM version (v2.9/v4.0), L1/L2/L5/L6/L8/L9/L10/L11/L254.
4. **mkvextract** — chapters from the MPLS with precise timestamps (only if the source has an MPLS).

MediaInfo and dovi_tool are optional: if they fail, the analysis goes on with mkvmerge's data.

For M2TS, the duration is resolved with a **mkvmerge → MediaInfo → ffprobe** cascade with a bitrate sanity check (it discards the absurdly short values typical of m2ts with a discontinuous PCR, where the tool only reads the first chunk).

### Extraction pipeline (Phases D+E)

- **Direct route** (when tracks are reordered or excluded): mkvmerge reads the source → final MKV in one pass, with tracks mapped by language + codec (not by position).
- **Intermediate route** (no reordering): intermediate MKV → mkvpropedit on the headers → `mv` to the output.

Real progress of the subprocess (`--gui-mode` parsed into `Progress: XX%`), atomic cancellation of the active subprocess, automatic final validation (`mkvmerge -J` + `mkvextract` on the resulting MKV, compared against what is configured in the session, including the codec tier of each audio track).

The job's phase strip is source-aware: for an ISO it shows mount → mkvmerge → unmount; for BDMV/M2TS the mount/unmount phases are shown as **skipped** (there is no actual mount).

---

## The "Inspect / Edit MKV" tab

### Unified file browser

Three roots:

- **📚 Library** (`/mnt/library`) — recursive from the root set in `LIBRARY_PATH`.
- **📦 Output** (`/mnt/output`) — the converter's output.
- **📥 Downloaded** (`/mnt/isos`) — the sources folder.

Selection by click + "Select" button (or double click). Incremental search inside the current directory. Navigable breadcrumb.

The left column lists every MKV analysed before. Reopening one is instant, because the analysis is cached by the file's content, not by its path: moving an MKV to another folder keeps its analysis.

And the column follows it. When an analysed MKV is no longer where it was analysed — typically moved from Output to the library — the app looks for it in the three folders by its content: same exact size, then the fingerprint of its first megabyte, never the name (on a QNAP the library holds thousands of thumbnail files also called `*.mkv`). Hidden, recycle-bin and snapshot folders are skipped, and so are symbolic links. When it turns up, the card points to the new path and a notice says so once; an open sub-tab on that MKV follows it too, matched by content and not by path. What can't be found stays marked, with a **Search again** button, and the app only says it is in none of the folders when the search covered everything — a folder that could not be read, too many same-size files, or a candidate that vanishes while it is being read (it is being moved), leave it undecided. **Cleanup** (⚙︎ Settings) uses the same search, without writing anything, and only offers to delete the analysis of an MKV that such a complete search found nowhere.

### In-place editing (no re-encoding)

Only metadata is changed, through `mkvpropedit` (instant):

- Rename audio and subtitle tracks.
- Change the `default` / `forced` flags.
- Edit chapters (interactive timeline, add/remove, rename, adjust timestamps).
- "Generic names" replaces custom names with `Chapter XX`.
- "Undo changes" goes back to the state of the analysis. "Close" warns about pending changes.

If the MKV is in `/mnt/library` (read-only), the app first copies it to `/mnt/output` with real progress and cooperative cancellation, and then applies mkvpropedit.

### DV+HDR deep dive

A detailed panel with five sections:

1. **Technical stream** — profile, CM version, frames, duration, FPS, scenes, bit depth, codec, RPU size, EL info.
2. **Mastering chain** — 3 cards (Master display / HEVC container / DV target L10) with primaries, peak/min nits, transfer + bit depth. A "P3 ↑ BT.2020" chip when there is a gamut expansion. Extra rows: DV trim targets (amber chips for L2 target_max_pq), HDR10 metadata (MaxCLL/MaxFALL from the SEI), L11 content type.
3. **Active area (L5)** — T/B/L/R offsets + active area + aspect ratio + symmetry, with an SVG frame view.
4. **CMv4.0 levels** (v4.0 only) — presence pills + logarithmic view of the L8 trim targets.
5. **DV L1 luminance profile** — a sparkline with 3 overlaid curves (peak/avg/min), reference lines (HDR10 MaxCLL/MaxFALL, L2 trims, L6 master), a hover tooltip with crosshair, and a mini card with percentiles (peak/p99/p95/p50/avg), the classification of frames by brightness (SDR-like <100 nits / midtone 100-300 nits / highlight ≥300 nits) and a distribution histogram.

### Extended analysis

A single **"Extended analysis"** button produces both the classified L8/L2 combos and the L1 luminance profile, from **one extraction of the RPU**:

1. `ffmpeg | dovi_tool extract-rpu` in a single pass, through a pipe (the intermediate HEVC is never written to disk).
2. `dovi_tool export` by levels → frame-by-frame parsing of L1 max_pq/avg_pq/min_pq, L2, L5, L6 and L8.

It takes around 10 minutes for a UHD film, almost all of it in the extraction. The result is cached with the rest of the analysis, so it is only paid once per file. MKVs produced by the CMv4.0 upgrade come out with the extended analysis already done, because the pipeline has already extracted their RPU. Multiplexed MKVs come out with the basic analysis done, and with the extended one too when the MKV their video comes from already had it: the video is copied unchanged, so its RPU — and with it the combos and the luminance profile — is the same, and the frame counts are checked to match before reusing it.

> **Note:** L1 max_pq values are the DV metadata encoded by the colorist — not the luminance you will see after tone mapping. Some films (Blade Runner 2049, among others) are tagged conservatively and can show low peaks even though real on-screen measurements are higher.

---

## The "Dolby Vision CMv4.0 upgrade" tab

A pipeline that injects a Dolby Vision CMv4.0 RPU (from DoviTools or other sources) into an MKV that carries the original Blu-ray's CMv2.9, adding the L8-L11 tone-mapping metadata with no re-encoding.

### The "New project" modal

Three tabs for the target source:

- **📦 DoviTools repo** (default) — a live listing of the public DoviTools Drive, fuzzy-matched against the MKV's file name. Direct download into the workdir.
- **🎬 Extract from MKV** — extracts the RPU from another MKV of yours that already has CMv4.0.
- **📁 Local folder** (legacy) — `.bin` files from `/mnt/cmv40_rpus`.

A recommendation banner classifies the title (feasible / not feasible / Not Sure!) according to the live DoviTools sheet.

### Blocking pre-flight

Before spending Phase A (~12 minutes to extract the HEVC of a UHD film), a quick pre-flight (~5 s for drive/path, ~30 s-2 min to extract from another MKV) downloads the bin and checks that it has CMv4.0:

- ✅ **Bin with CMv4.0** → Phase A starts, and Phase B reuses the bin from the workdir (no new download).
- ❌ **Bin without CMv4.0** (v2.9, e.g. "P5 to P8 transfer") → aborts before Phase A, with a clear message in the project's log. Saving: ~12 minutes.

Asynchronous (`running_phase="preflight"`), blocking (other phases do not start in parallel), cancellable. If it fails (for example, because Google Drive's download quota is exhausted), the project offers to retry the pre-flight on its own, without starting Phase A.

### Backend-driven auto-pipeline

When auto mode is on (the toggle in the "New project" modal), the server itself chains every phase that does not need manual input: pre-flight → Phase A → Phase B → trust gates → (Phase C/D only if the gates do not pass) → Phase F → Phase G → Phase H → done. The client takes no part in the orchestration.

The job survives the Mac going to sleep, the tab being closed, the browser crashing or the Wi-Fi dropping. The state is persisted atomically in `/config/cmv40/{session_id}.json` after each phase, and the log is appended to its own file, `/config/cmv40/{session_id}.log`. When the app is reopened, the panel is hydrated from disk and reattaches to the log WebSocket in ~1-3 seconds.

Recovery after a server restart: sessions that had `running_phase != null` are marked as an error with the message "Session interrupted by a server restart" (intended behaviour — they are not resumed automatically, to avoid unwanted starts after a kill).

### Pipeline phases

1. **Analyse source** — ffmpeg + dovi_tool extract-rpu from the source MKV, in a single pass through a pipe.
2. **Provide target RPU** — drive / mkv / path. Reuses the pre-flight if it passed.
3. **Extract BL/EL** — dovi_tool demux + sampled per-frame data (skipped with drop-in bins).
4. **Verify sync** — an interactive chart with two curves, zoom by selection, automatic offset detection, cumulative corrections and a confidence metric (Pearson correlation on MaxCLL).
5. **Apply correction** — `dovi_tool editor` with `remove`/`duplicate`, at the start or at the end of the film (part of Phase D; it does not advance the phase).
6. **Inject RPU** — drop-in with `inject-rpu` (the bin replaces the RPU whole) or frame-by-frame merge with `dovi_tool editor --allow-cmv4-transfer`. The levels transferred depend on the source workflow:
   - **Source P7 FEL** → `[1, 2, 3, 6, 8, 9, 10, 11, 254]` (L1/L2/L6 included on purpose — the restored WEB grading refines the BD's legacy L1 stats)
   - **Source P7 MEL / P8** → `[3, 8, 9, 11, 254]` (only CMv4.0-exclusive levels + L3 + marker; the BD's L1/L2/L5/L6 are kept because they describe your pixels)
7. **Final remux** — `dovi_tool mux` + mkvmerge, keeping audio/subtitles/chapters.
8. **Check and save the MKV** — fast path for drop-in (ffprobe + mkvmerge -J, a few seconds), or a FULL extract-rpu of the pre-mux HEVC with strict validation for merge (frame count ±2 vs expected, CM v4.0, el_type according to the workflow, **L8 present**). If it fails, the `.mkv.tmp` is kept for inspection.

### Trust system

After Phase B, the bin is classified into a `target_type`:

| Type | Detection | Behaviour |
|---|---|---|
| `trusted_p7_fel_final` | P7 FEL + CMv4.0 + L8 + gates OK | Drop-in: skips Phase D + skips the merge in F |
| `trusted_p7_mel_final` | P7 MEL + CMv4.0 | Skips Phase D if the gates are OK |
| `trusted_p8_source` | P8 + CMv4.0 + L8 | Skips Phase D if the gates are OK; Phase F merges |
| `generic` | P8 without L8 / P5 | Full flow (visual review + merge) |
| `incompatible` | CMv2.9 | Aborts (covered by the pre-flight) |

Critical gates: `frames` (Δ=0), `cm_version` (v4.0), `has_l8`, `l5_div` (≤30 px). Soft gates: `l6_div` (±50 nits), `l1_div` (±5%).

Manual override: `trust_override = "force_interactive"` forces the full route even when the bin is trusted.

### L8 validation after the merge (Phase H)

When the classic route (merge) reaches validation, a full `dovi_tool extract-rpu` runs on the pre-mux HEVC and `has_l8 == True` is required in the resulting RPU. It is defence in depth against a hypothetical `dovi_tool editor` bug that would leave the RPU tagged as v4.0 but without the L8 trims that make the upgrade worthwhile. If it fails, the `.mkv.tmp` is kept for inspection.

### Keep MKV / Inject RPU decision model

After downloading the bin, the app runs `dovi_tool export` and measures how much colorist work the L8 really carries (it trusts neither the file name nor the repository's tag):

1. **Does the bin have a worked L8?** The deciding figure is the largest deviation of the L8 trims from neutral. Above 50 units, the trims were made by a colorist; at or below, they are what an automatic analysis would produce from your own disc. L8 is the only level that comes from the colorist alone — L1 and L3 are generated by Dolby's analysis — which is why it is the only one that decides.
   - No worked L8 → **Keep the current MKV** (a CMv4.0-capable player does the conversion on the fly with the same result). If the bin still carries real L3/L9/L11 metadata, the app says so and leaves the choice to the user.
2. **Which route?** The workflow matrix decides (source workflow × target type × trust): a P7 FEL source with a trusted P7 FEL CMv4.0 bin goes **drop-in** (**Inject RPU, fast**, the bin replaces the RPU whole); every other case goes through the **merge** (**Inject RPU, keeps L2**), transferring only the CMv4.0 levels.

Quality classification of the bin (added as a suffix to the final MKV's file name):

| Tier | File name label | Criterion |
|---|---|---|
| FULL | `[CMv4 FULL]` | `target_mid_contrast` or `clip_trim` populated with non-neutral values (CMv4.0-exclusive fields). "FULL minimal" sub-variant when there are few unique combos (≥3) but with significant trims (>50 units from neutral) |
| CORE+ | `[CMv4 CORE+]` | Intense dynamic grading (≥0.1 combos/shot or ≥400 absolute) |
| CORE | `[CMv4 CORE]` | Standard streaming master with basic trims |
| (synthetic) | — | The bin adds nothing over on-the-fly conversion; recommendation: keep |

The user sees the recommendation in a card on the project panel, with a colour badge and a readable reason. When the recommendation is "keep", two buttons appear: `[✓ Keep current MKV]` (closes the project without touching the MKV) and `[🔬 Inject RPU anyway]` (the user's override, to process it anyway — useful to archive the full CMv4.0 version).

The session's `output_workflow` field records how each project ended: `keep_cmv29` (keep) · `restore_dropin` (drop-in) · `restore_merge` (selective merge).

### Retroactive audit (`tools.audit_cmv40_bins`)

A CLI tool to review every historical CMv4.0 session and reclassify it with the current model:

```bash
docker exec hdo-iso-converter python3 -m tools.audit_cmv40_bins

# Only wasted jobs (processed with synthetic bins)
docker exec hdo-iso-converter python3 -m tools.audit_cmv40_bins --filter-keep

# Download the bins again from Drive if they are no longer in the workdir
docker exec hdo-iso-converter python3 -m tools.audit_cmv40_bins --redownload

# Detail of one case: dumps the L8 combos with their deltas from neutral
docker exec hdo-iso-converter python3 -m tools.audit_cmv40_bins --detail "Black Phone 2"
```

Useful to calibrate the classifier's thresholds or to decide which historical jobs to redo.

### CMv4.0 recommendation (live sheet + Drive)

- **DoviTools sheet** parsed live (XLSX + openpyxl to keep the hyperlinks; fallback to HTML/CSV/disk cache).
- **Fuzzy match** of the file name: max(SequenceMatcher, token-set Jaccard, containment) on accent-stripped text, with normalisation of Roman numerals and stop words.
- **Multiple TMDb candidates** (top 5) with adaptive thresholds depending on whether the year is present.
- **Dedup** by (slug, year) — tells apart, for example, The Lion King 1994 and 2019.

---

## The "Multiplex MKVs" tab

Two MKVs, **A** and **B**, and from each whatever you choose — the video, audio tracks, subtitles, chapters and attachments — in one final MKV. The case that started it: a UHD remux at **23.976 fps** and a WEB-DL at **24.000 fps**. Muxed as they are, the audio drifts by 0.1 %: −0.30 s at minute 5, −3.00 s at minute 50, **−5.76 s at minute 97**. Nothing in the files says so — their durations differ because of the credits, and the chapters do not show it — so the tab measures before it multiplexes.

A and B have no fixed roles. A project starts **with nothing chosen** and is filled in section by section — Video, Audio, Subtitles, Chapters, Other —, each with two columns (A | B) and a switch per item; before multiplexing, the sections left empty are pointed out in a dialog, so a twenty-minute job is not repeated for an oversight.

### Measuring the sync (`phases/mux_sync.py`)

- One audio track from each file (the same language when there is one — the original version, from TMDb, first, then English: the same language is not always the same dub), seven 30 s windows between 5 % and 88 % of the movie, decoded to mono at 4 kHz and reduced to a 100 Hz onset envelope — independent of the codec, the level and the mix.
- Normalised cross-correlation by FFT, in pure Python. Two anchor points search ±60 s; the rest search ±6 s around where the anchors' rate puts them, with a wide retry for a cut.
- B's envelope is rescaled to A's timeline before correlating, so a 23.976/24 pair does not smear the peak.
- A straight line through the valid points, always **B relative to A** — the sound that is at `t_A` in A is at `t_B = α·t_A + β` in B — gives the verdict: `en_sincronia`, `retraso` (constant offset), `otro_ritmo` (different speeds, explained by the frame rates), `deriva` (they drift apart at a rate nobody declares), `montaje_distinto` (a jump: a different cut) or `no_concluyente` (an uncertain measurement).

On the real case: different speeds, α = 0.999006491, final frame rate **23.999868 fps**, β = 3 ms (below 10 ms it is noise), spread 4.5 ms, 7/7 points, in 18 s.

### The plan (`phases/mux_plan.py`, pure)

What the timeline does depends on **where** each thing comes from. With **v** the MKV the video comes from and **o** the other, the mapping from v to o is `t_o = f·t_v + d`, with `(f, d) = (α, β)` when v = A and `(1/α, −β/α)` when v = B. The video itself never gets a `--sync`: only its frame duration changes.

| situation | what happens |
|---|---|
| nothing timed comes from o | no measurement needed: everything goes as it is |
| same speed | o's tracks and chapters are shifted (`--sync`) |
| different speeds, audio only from v | o's tracks are brought to v's timeline |
| different speeds, audio only from o | **the video's speed is adjusted** to o's (`--default-duration`): v's other tracks are stretched with it, o's are shifted |
| different speeds, audio from both | not multiplexed: audio cannot be stretched without re-encoding |

Adjusting the speed is container-only: mkvmerge does not rewrite the HEVC VUI, and the Dolby Vision RPU and the FEL go through untouched. A variable-frame-rate video, or one whose base and enhancement layers sit in tracks with different frame durations, is never adjusted — nor is one whose frame-rate mode MediaInfo could not read when the project was created: without it there is no telling whether it is variable, and creating the project again reads it again. A different cut, a drift nobody declares or an uncertain measurement can be multiplexed anyway, but only after the user accepts it in a dialog. When the measurement is uncertain, the delay of B relative to A can be typed in.

Chapters are chosen one by one, from A, B or both: the plan maps each one to the final timeline, merges them in film order, drops what falls outside the video and moves the last one before zero to the start. They are composed into a single XML with **the UIDs removed** — mkvmerge silently drops a chapter whose UID repeats one already seen (measured with version 88), and a WEB-DL usually copies the disc's chapters — and nested chapters are trimmed the same way. When all the chapters of one file are chosen and that file is not shifted, mkvmerge copies them as they are — unless one of them, or one nested inside, would start past the end of the video.

The selection patterns ("All" and "None" per column, "My languages", "Forced only", "Fonts for the chosen ASS") are resolved by the server on the saved project (`POST /api/mux/{id}/patron`); the interface waits for any pending save before asking.

### Three phases in one queue turn (`phases/mux_pipeline.py`)

1. **Source check** — both files still there and unchanged, the plan still allows it, the output name free, enough room in Output.
2. **Track multiplexing** — one `mkvmerge` into a `.mkv.tmp`, never under the final name, with the chapters in a composed XML when they are mixed or shifted. A `.mkv.tmp` with that name that belongs to someone else — the one another multiplex kept after a failed check, or a CMv4.0 remux waiting for its validation — stops the job instead of being deleted; each project records the working file it wrote.
3. **Check and save** — the multiplexed file read back against the plan: every track (the video included: name and flags too), the video's frame rate, frame count and duration (the proof that its speed was adjusted), the chapters to the millisecond, the attachments, the Dolby Vision of the chosen video and the sync of every audio track from o against the track of the video's MKV that the measurement used, at four points: a different dub only correlates through the music and effects, and two points were not enough. If it all matches, it is published without overwriting anything and left with its "Inspect / Edit MKV" analysis already done (see "Extended analysis"); if not, the `.mkv.tmp` stays for inspection.

Projects saved before the redesign (with the video fixed in A) carry no `esquema` and are not loaded; the tab never reached a release, so they only existed on the author's NAS.

### The interface (`static/mux.js`)

Same frame as the other tabs: a project column, navy sub-tabs, stacked cards and the shared Jobs column. The panel opens with a summary — what each MKV contributes, the plan in words, the output name and the Multiplex button —, then the sync card and the five sections. Edits save themselves; each one sends its whole field (the full track list, the video first, then audio, then subtitles) and the format version (`esquema`), so a browser tab running the old code gets "reload the page" instead of saving a selection without the video. Every read carries a sequence number so an old response never overwrites a newer one, and a rejected change is undone by reading the project again. The sync card draws the measured points with the fitted line, but only when a single line describes the movie.

---

## Technical stack

- **Backend:** Python 3.12 (ubuntu:24.04, in a virtualenv), FastAPI, uvicorn
- **Frontend:** vanilla JS ES6+, Sortable.js (CDN), no framework and no build step
- **Tools:** MKVToolNix 103 (mkvmerge, mkvpropedit, mkvextract), MediaInfo 26.10, ffmpeg/ffprobe 7.1.5 (BtbN static build), dovi_tool 2.3.4 — all pinned in the Dockerfile, and each bump goes through the real-file regression (`app/tools/regresion_real.py`)
- **ISO access:** direct loop mount (`mount -t udf -o ro,loop`) — requires `privileged: true` in Docker
- **External integrations:** TMDb (posters/synopses/episodes) and the DoviTools repository link — **included, they work out of the box**; the Google API key (Drive v3 + Sheets v4 / openpyxl) is the only thing to configure, and it unlocks the repository
- **Languages:** the interface, the server's log and the manual are available in English, Spanish and Catalan

## Project structure

```
hdo-iso-converter/                ← repository / Docker image (technical name)
├── .github/workflows/
│   ├── publish-docker.yml        ← Publishes the image to ghcr.io on every release
│   └── tests.yml                 ← The test suite on every push and PR
├── docker/
│   ├── Dockerfile
│   ├── docker-compose.yml
│   ├── .env.example
│   └── entrypoint.sh
├── docs/
│   ├── TECHNICAL.md              ← This document
│   └── img/                      ← The README's screenshots
├── app/
│   ├── main.py                   ← FastAPI app + app-wide endpoints + startup
│   ├── models.py                 ← Pydantic models
│   ├── storage.py                ← JSON persistence + fingerprint
│   ├── paths.py                  ← The app's directories, in one place
│   ├── workload.py               ← Admission control: one heavy job at a time
│   ├── analysis_progress.py      ← The analysis step (shared by Tab 1 and Tab 2)
│   ├── routers/
│   │   ├── tab1.py               ← Tab 1: endpoints + pipeline orchestrator + WS
│   │   ├── tab2.py               ← Tab 2: /api/mkv/* + browser + quality + apply
│   │   ├── cmv40.py              ← Tab 3: endpoints + orchestration
│   │   └── mux.py                ← Tab 4: /api/mux/* + the sync measurement + the patterns + the queue
│   ├── queue_manager.py          ← The single job queue
│   ├── dev_fixtures.py           ← Fixtures when DEV_MODE=1
│   ├── phases/
│   │   ├── iso_mount.py          ← UDF loop mount/umount + Source abstraction
│   │   ├── phase_a.py            ← Full analysis of the source
│   │   ├── phase_b.py            ← Automatic selection rules
│   │   ├── phase_d.py            ← mkvmerge extraction from the source
│   │   ├── phase_e.py            ← Final write + validation
│   │   ├── mkv_analyze.py        ← Tab 2: MKV analysis + editing
│   │   ├── rpu_analyze.py        ← Tab 3: L2/L8 analysis of the bin
│   │   ├── luminance.py          ← The L1 luminance profile
│   │   ├── cmv40_strategy.py     ← Tab 3: workflow × target × trust matrix (pure)
│   │   ├── cmv40_pipeline.py     ← Tab 3: the full CMv4.0 pipeline
│   │   ├── mux_sync.py           ← Tab 4: the sync, measured from the audio
│   │   ├── mux_plan.py           ← Tab 4: the timeline, the chapters, the patterns and what blocks multiplexing (pure)
│   │   ├── mux_pipeline.py       ← Tab 4: reading the sources and the three phases
│   │   └── mux_relato.py         ← Tab 4: what the project is doing, in words
│   ├── services/
│   │   ├── settings_store.py     ← /config/app_settings.json
│   │   ├── tmdb.py               ← TMDb search + details (movies + TV)
│   │   ├── rec999_sheet.py       ← Live DoviTools sheet
│   │   ├── rec999_drive.py       ← Drive API v3 listing + download
│   │   ├── rec999_drive_match.py ← Fuzzy match file name → bin
│   │   └── cmv40_recommend.py    ← Orchestrates file name → TMDb → sheet
│   ├── i18n/                     ← Server catalogs (es/en/ca)
│   ├── tools/
│   │   └── audit_cmv40_bins.py   ← CLI: retroactive audit
│   └── static/
│       ├── index.html
│       ├── core.js · settings.js · cmv40_modals.js
│       ├── tab1.js · tab2.js · tab3.js · mux.js · browser.js · workbar.js
│       ├── i18n/                 ← Interface and manual catalogs (es/en/ca)
│       └── style.css
├── archive/                      ← Historical specs
├── CLAUDE.md
├── README.md
└── run_local.sh
```

## REST API (summary)

### Tab 1 — Create MKV
- `GET /api/sources` — sources in `ISOS_PATH`, classified by type (iso / bdmv_folder / m2ts)
- `POST /api/check-duplicate` — fingerprint + list of existing sessions
- `POST /api/disc-probe` — classifies the source and identifies episode candidates (accepts `media_type_hint`)
- `GET /api/disc-probe/progress` — polling of the probe's current step
- `GET /api/tv-search` · `/api/tv-details/{id}` · `/api/tv-season/{id}/{n}` — TMDb TV endpoints
- `POST /api/create-series-sessions` — creates N sessions at once (accepts `mode`: `add_only` / `replace` / `skip_existing`)
- `POST /api/analyze` — movie flow (Phase A+B)
- `GET/PUT/DELETE /api/sessions/{id}`, `POST /api/sessions/{id}/execute|cancel|reapply-rules|reset-chapters`
- `WS /ws/{id}` — the job's live log

### Tab 2 — Inspect / Edit MKV
- `GET /api/library/browse?root=library|output|downloaded&path=...` — file browser
- `GET /api/mkv/recientes` — the MKVs analysed before (left column); the ones no longer at their path are looked for by content and repointed (`?buscar=true` forces the search)
- `POST /api/mkv/analyze` — analysis (mkvmerge -J + MediaInfo + dovi_tool)
- `POST /api/mkv/quality-audit` — extended analysis (L8/L2 combos + luminance profile)
- `GET /api/mkv/quality-audit/progress` · `POST /api/mkv/quality-audit/cancel` — its progress and cancellation
- `POST /api/mkv/apply` — applies the edits (mkvpropedit + copy from the library if needed)
- `POST /api/mkv/apply/cancel` — cooperative cancellation of the copy
- `GET /api/mkv/apply/progress` — polling of the copy + edit

### Tab 3 — CMv4.0
- `GET /api/cmv40`, `POST /api/cmv40/create` (accepts `auto_pipeline` + `pending_target` so that the backend orchestrates on its own), `GET/DELETE /api/cmv40/{id}`
- `POST /api/cmv40/{id}/auto-pipeline` — toggles auto mode mid-pipeline
- `POST /api/cmv40/{id}/preflight-target` — blocking pre-flight (kind: drive | path | mkv); with auto on, it chains Phase A when it ends
- `POST /api/cmv40/{id}/retry-preflight` — retries the pre-flight of the bin already chosen
- `POST /api/cmv40/{id}/analyze-source` — Phase A
- `POST /api/cmv40/{id}/target-rpu-{path|from-mkv|from-drive}` — Phase B
- `POST /api/cmv40/{id}/extract` — Phase C
- `GET /api/cmv40/{id}/sync-data` — the Phase D chart's data
- `POST /api/cmv40/{id}/{apply-sync|reset-sync|mark-synced|inject|remux|validate|cleanup}` — the following phases
- `POST /api/cmv40/{id}/accept-keep` · `POST /api/cmv40/{id}/override-recommendation` — the Keep / Inject decision
- `POST /api/cmv40/{id}/cancel` — kills the active subprocess
- `WS /ws/cmv40/{id}` — live log

### Tab 4 — Multiplex MKVs
- `GET /api/mux` — the projects (left column)
- `POST /api/mux/create` — reads both MKV files and creates the project with nothing chosen (`{a, b}`); its step is in `GET /api/mux/create/progress`
- `GET/PUT/DELETE /api/mux/{id}` — the project with its `plan`, `relato`, `patrones` and `mis_idiomas`; `PUT` (always with `esquema: 2`) changes the tracks, chapters, attachments, names and decisions
- `POST /api/mux/{id}/patron` — applies a selection pattern to a section (`{esquema, seccion, patron}`), on the saved project
- `POST /api/mux/{id}/medir` · `POST /api/mux/{id}/medir/cancelar` — measures the sync (in the background) and stops it
- `POST /api/mux/{id}/ejecutar` · `POST /api/mux/{id}/cancelar` — queues the multiplex, and cancels it or takes it out of the queue; once the MKV is published (only its analysis is left) cancelling is refused with `{ok: false, publicado: true}`
- `POST /api/mux/{id}/tmdb-refresh` — finds the film's card again

### Shared
- `GET /api/trabajos` · `GET /api/historial` — the Jobs column: running, queued and finished jobs
- `GET/POST /api/settings` — API key configuration (TMDb / Google). The TMDb key is an override: the app ships with its own
- `GET /api/health` — healthcheck
- `GET /api/version` · `GET /api/version/check-updates` — current version + check for updates against GitHub Releases

## Host requirements

- Docker with `privileged: true` support (for the loop mount of UDF 2.50 ISOs)
- amd64/x86_64 architecture
- Space in `/mnt/tmp` ≥ 2× the size of the largest MKV you will process (CMv4.0 workdir)
- Space in `/mnt/output` for the finished MKVs

## Local development

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r app/requirements.txt

# Development mode with fake data (no real ISOs):
echo "DEV_MODE=1" > .env.local
./run_local.sh
```

`DEV_MODE=1` turns on fixtures: fake ISOs, simulated sessions, fake RPUs. Useful to iterate on the UI without UHD discs.

The test suite runs from the repository root:

```bash
python3 -m unittest discover -s app/tests
```

### Upgrading a tool

MKVToolNix, MediaInfo, dovi_tool and ffmpeg are pinned in `docker/Dockerfile` (with a checksum where the download is a file), and the build checks that what got installed is what was pinned. Raising one is three steps:

1. Change its `ARG` (and its SHA-256); dovi_tool's version is also in `docker/docker-compose.yml`, whose build args win over the Dockerfile.
2. Re-capture the tools' real outputs for the fake binaries' contract: `app/tools/capturar_contrato.py`, run inside the new image (`app/tests/test_contrato_de_los_fakes.py` fails until it matches the pinned versions).
3. Run the real-file regression, `app/tools/regresion_real.py`, with the old image and the new one over the same files, and compare: it runs the app's own analysis, rip, edit, Dolby Vision and multiplex code on real MKVs, m2ts and RPUs, and says what changed in what the app derives from them.

## License

The code of this application is distributed under the **MIT** license — see
[`LICENSE`](../LICENSE).

The Docker image also contains third-party software with its own licenses,
among them **GPL-2.0** (MKVToolNix) and **GPL-3.0** (FFmpeg). The application
invokes those tools as separate processes and does not link against their
libraries, so their copyleft does not reach the app's code; but distributing
the image does carry obligations over those binaries.

Everything is in [`THIRD-PARTY-NOTICES.md`](../THIRD-PARTY-NOTICES.md): the inventory
of components with their version and license, the required copyright notices,
the full texts in [`app/static/licenses/`](../app/static/licenses/) and the
written offer of source code. In the running app, the same is served under
`/static/licenses/` and linked from ⚙︎ Settings → About.

> This product uses the TMDB API but is not endorsed or certified by TMDB.
