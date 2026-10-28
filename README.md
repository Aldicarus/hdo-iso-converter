<div align="center">

# UHD Blu-ray Toolkit

**The best version of every film, in a single MKV.**

Rip the disc &nbsp;→&nbsp; Upgrade it to CMv4.0 &nbsp;→&nbsp; Add audio and subtitles from other releases &nbsp;→&nbsp; Verify the result

One web app for the whole remux workflow. It runs the tools you already know,<br>
checks every step, and never delivers a file that is worse than the one you started with.

[![Latest release](https://img.shields.io/github/v/release/Aldicarus/hdo-iso-converter?sort=semver&filter=v*&label=release)](https://github.com/Aldicarus/hdo-iso-converter/releases/latest)
[![Docker](https://img.shields.io/badge/docker-ghcr.io-2496ED?logo=docker&logoColor=white)](https://github.com/Aldicarus/hdo-iso-converter/pkgs/container/hdo-iso-converter)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

[How it works](#how-it-works) · [Same tools, one more layer](#same-tools-one-more-layer) · [The four steps](#the-four-steps-in-detail) · [Installation](#installation) · [Your first film](#your-first-film) · [FAQ](#faq)

> 🖼️ **[Screenshot 1 — hero]** *A CMv4.0 upgrade project of a well-known film: the film's card, the DoviTools verdict, the recommendation and the Jobs column with a job running.*

</div>

---

## Why

No single release has the best version of a film.

- **The UHD Blu-ray has the best picture.** It has the highest bitrate available, and on many discs Dolby Vision **Profile 7 FEL**: a full enhancement layer that carries picture information the base layer and streaming releases do not have.
- **Streaming has the newest grading metadata.** Many streaming masters carry Dolby Vision **CM v4.0**, with trims the colorist made by hand for each kind of display. The disc only carries the older CM v2.9.
- **Another release often has the language you want.** When the disc lacks a dub or subtitles, a WEB-DL usually has them, with a weaker picture and often at a slightly different speed.

UHD Blu-ray Toolkit takes the best of each and puts it in one MKV. Picture and sound are never re-encoded: they are copied bit for bit, and the only things that change are the Dolby Vision metadata and the container.

It runs on a NAS or any Linux machine with Docker and is used from the browser.

---

## How it works

> 🖼️ **[Diagram]** *The three sources and the four steps: what each source brings, what each step does and what it checks.*

Four steps, one tab each. Together they cover what it takes to improve a remux: the best picture, the best grading metadata, the language you want, and proof of what ended up inside the file.

| | Step | What it does | What it checks |
|---|---|---|---|
| 1 | 💿 **Blu-ray ISO → MKV** | Turns the disc into a lossless MKV with the Dolby Vision FEL layer, the best audio tracks and the chapters | The finished MKV, track by track and chapter by chapter, against what was configured |
| 2 | ✨ **Dolby Vision CMv4.0 upgrade** | Adds the colorist's CMv4.0 trims to the disc's own Dolby Vision, without touching the picture | Whether the upgrade is worth it, before it starts; the sync frame by frame; the Dolby Vision of the delivered file |
| 3 | 🔀 **Multiplex MKVs** | Puts the dub and subtitles of another release next to the disc's picture | The sync, measured from the sound; every track and every audio's sync in the result |
| 4 | 🔬 **Inspect / Edit MKV** | Shows everything inside any MKV, and edits track names, flags and chapters in seconds | Nothing is assumed: this is the step where you see it for yourself |

Each step also works on its own: upgrade an MKV you already have, multiplex two releases without ripping anything, or inspect any file in your library.

---

## Same tools, one more layer

UHD Blu-ray Toolkit does nothing new to your files. Every change is made by **mkvmerge**, **mkvpropedit**, **dovi_tool** or **FFmpeg**, with the same options you would type yourself, and the log of each job shows the commands that were run. What the app adds is everything around those commands:

- **It orchestrates.** It picks the route each case needs, runs the steps in order and one heavy job at a time, so they do not compete for the same disks. Jobs keep running after you close the browser, and the queue resumes after a restart.
- **It checks before spending time.** Is the CMv4.0 file really CMv4.0, and were its trims made by a colorist? Do its frames line up with your disc? Is there room on the disk, and is the output name free? Each question is answered before the 20 to 40 minutes of work, not after.
- **It verifies after.** Each step reads back the file it produced and compares it with what it was meant to produce.
- **It keeps you informed.** Progress comes from the tools themselves, and a figure that is only an estimate says so. A Jobs column shows what is running, how long is left and everything that has finished, with its log.

### Never worse than your source

- **Nothing is re-encoded.** Picture and sound are copied bit for bit.
- **Your originals are never modified.** Discs and the library are mounted read-only. An edit to a library file is made on a copy.
- **A file that fails its checks is not delivered as finished.** Each MKV is written under a temporary name and only takes its final name once it has passed. The upgrade and the multiplex never overwrite an existing file.
- **When a step cannot improve the file, the app says so.** A CMv4.0 file without colorist trims gets the recommendation to keep your MKV. Frames that do not line up stop the upgrade, and a sync that cannot be measured stops the multiplex, until you decide.

### By hand, or here

| | By hand | With UHD Blu-ray Toolkit |
|---|---|---|
| **Rip** | Find the main playlist, read `mkvmerge -J`, choose the track IDs, write the track order, names and flags, extract the chapters, then check the result | Select the ISO. The title, the tracks and the chapters are chosen with the reason for each choice, and the result is checked against them |
| **Upgrade** | Look the film up in the DoviTools sheet, find the file in the repository, check its CM version and its L8 trims, demux the disc, compare frame counts, write the editor JSON, choose which levels to merge, inject, mux and remux, then check the profile | Select the MKV and the file the repository suggests. The rest runs by itself, and stops where a decision is yours |
| **Multiplex** | Line up the two releases by ear or by waveform, work out the delay and the speed factor, write the `--sync` and `--default-duration` options, and check the drift at the end | Select the two MKVs and what you want from each. The sync is measured, the timing is computed, and every audio track is checked in the result |
| **Verify** | Run MediaInfo, `dovi_tool info` and `dovi_tool export`, and read the output | Open the MKV |

### Is it for you?

**If you want control over every option**, use the command-line tools directly. Together with [DoVi_Scripts](https://github.com/R3S3t9999/DoVi_Scripts), they can do everything this app does to a file — this app runs them.

**If you would rather have the whole process run for you**, with every step checked and a result that is never worse than the file you started with, this is the tool.

---

## The four steps in detail

### 1 · Rip the disc — 💿 Blu-ray ISO → MKV

> 🖼️ **[Screenshot 2]** *An ISO → MKV project: the Dolby Vision and HDR cards, and the tracks kept and discarded, each with its reason.*

- **The FEL layer, preserved.** The base layer, the enhancement layer and the Dolby Vision metadata go into the MKV as they are on the disc. Before anything is ripped, the analysis tells you whether the disc carries a full (FEL) or a minimal (MEL) enhancement layer.
- **The right title.** The main title is chosen by what it plays, not by the size of its playlist. When a disc has several cuts of the film, or decoy playlists, the app tells you before the rip.
- **The right tracks, with the reason for each.** The best audio format for your language and for the original language (TrueHD Atmos, DTS-HD MA…), the full and forced subtitles, and the disc's chapters. Every discarded track stays visible with the reason it was left out, and can be brought back with one click.
- **ISO images, BDMV folders or M2TS files**, read directly, without copying or extracting them first. TV shows too: each episode on the disc becomes its own MKV, matched to its season on TMDb.
- **Checked against what was configured.** Track by track — language, codec, channels, name, default and forced flags — and chapter by chapter, to the millisecond.

### 2 · Upgrade it to CMv4.0 — ✨ Dolby Vision CMv4.0 upgrade

> 🖼️ **[Screenshot 3]** *The analysis and recommendation card: what the CMv4.0 file brings, level by level, next to what your disc already has.*

Dolby Vision carries a set of instructions that tells each display how to fit the picture to what its screen can show. Those instructions are where the colorist's work lives, and CM v4.0 adds trims made by hand for each kind of display. The upgrade takes the CMv4.0 trims from a streaming master and adds them to your disc's Dolby Vision. The picture, its FEL layer and its bitrate stay as they are.

- **It finds the file.** It reads the DoviTools community sheet live and searches its repository for the file that matches your film, with the community's notes on it.
- **It tells you whether the upgrade is worth it, in seconds and before anything runs.** It checks that the file really is CMv4.0 and measures whether its L8 trims were made by a colorist or generated by an analysis. With a colorist's trims, it recommends the upgrade. Without them, it recommends keeping your MKV. When the file only carries analysis metadata, it tells you what you would gain and leaves the decision to you.
- **It compares the file with your disc.** Frame count, active area (L5) and brightness metadata (L1, L6). When the two masters disagree on brightness, your disc's metadata is kept and only the CMv4.0 trims are taken from the file.
- **It checks the sync frame by frame.** If the frames do not line up, a chart shows where the two drift apart, and the correction can be applied at the start or at the end. The upgrade does not go on with the frames out of alignment unless you confirm it.
- **The FEL layer stays.** On FEL discs the CMv4.0 metadata is merged into the disc's own and the enhancement layer is kept. On MEL discs the minimal layer, which carries no picture information, is dropped and the result is Profile 8.1. Each case takes its own route — drop-in, merge or direct injection — without you having to choose it.
- **The delivered file is checked**: the frame count, and the Dolby Vision profile, layer and CM version read from the MKV itself, along with every audio and subtitle track of the original. The file name carries the quality tier the trims reach: `[CMv4 FULL]`, `[CMv4 CORE+]` or `[CMv4 CORE]`.

> 🖼️ **[Screenshot 4]** *The sync chart: the brightness of the disc and of the CMv4.0 file, frame by frame, with the confidence of the alignment.*

### 3 · Add audio and subtitles from other releases — 🔀 Multiplex MKVs

> 🖼️ **[Screenshot 5]** *The sync measurement of two releases at different speeds: the seven points, the drift and the speed adjustment.*

- **Two MKVs, and you choose what comes from each.** Video, audio, subtitles, chapters and attachments, section by section, and chapters one by one. Typically the remux picture, with its Dolby Vision FEL, and the other release's dub and subtitles.
- **The sync is measured, not assumed.** The sound of both files is compared at seven points of the film. Two releases often run at slightly different speeds — 23.976 and 24 fps — and merged as they are, the audio drifts by almost six seconds by the end.
- **Different speeds, solved in the container.** The video's speed is adjusted by changing the timing of each frame, whichever file the video comes from. The picture and its Dolby Vision stay as they are, bit for bit, and the dub stays in sync from the first minute to the last.
- **It says when it cannot be done.** A different cut, a video without a constant frame rate, audio from both releases at different speeds, or a measurement that does not add up: each one is shown before anything is multiplexed. A section left empty by mistake is pointed out before the job starts.
- **The result is checked**: every track, the chapters to the millisecond, the Dolby Vision of the video, and the sync of every audio track at four points of the film.

### 4 · Verify the result — 🔬 Inspect / Edit MKV

> 🖼️ **[Screenshot 6]** *The DV+HDR deep dive: the Dolby Vision profile and levels, the mastering chain and the film's brightness from start to finish.*

- **Everything inside the file:** the Dolby Vision profile, its FEL or MEL layer, the CM version and its levels, the mastering chain and the HDR10 metadata, along with every audio format and its real bitrate.
- **The film's brightness from start to finish**, in an interactive chart with its peaks, percentiles and how much of the running time falls at each brightness level.
- **The CMv4.0 analysis**, which shows what the colorist's trims add and the quality tier they reach.
- **Ready when the file is.** The MKVs that come out of the upgrade and the multiplex arrive with their deep dive already done, and a file moved to the library afterwards is found again by its content.
- **Instant editing** of track names, default and forced flags, and chapters, with no re-encoding and in seconds. MKVs in the library are edited on a copy, so the original is never touched.

---

## Always in view

> 🖼️ **[Screenshot 7]** *The Jobs column and the detail of a running job: its phases, the measured progress, the time left and the log with the commands.*

- **The Jobs column** stays next to all four tools: what is running, what is queued, how long each job has taken and how long is left, and the history of everything that has finished.
- **The browser tab tells you when a long job finishes**, even when you are working in another tab.
- **A card for every film**, with its poster, synopsis and TMDb rating.
- **A manual built into the app** explains Dolby Vision and its profiles, the DoviTools sheet and repository, and every route the upgrade can take.
- **English, Spanish and Catalan**, picked up from the browser the first time, and **light and dark mode**, following the system setting.

---

## Installation

**Requirements:** a NAS (such as a QNAP with Container Station) or a Linux computer with **Docker** and a 64-bit Intel or AMD processor. The app ships as a ready-made image, so there is nothing to compile.

**1. Download the two configuration files** into a new folder:

```bash
mkdir uhd-bluray-toolkit && cd uhd-bluray-toolkit
curl -LO https://raw.githubusercontent.com/Aldicarus/hdo-iso-converter/main/docker/docker-compose.yml
curl -L  https://raw.githubusercontent.com/Aldicarus/hdo-iso-converter/main/docker/.env.example -o .env
```

**2. Set your folders** in the `.env` file. Each folder must exist before the app starts:

| In `.env` | Folder |
|---|---|
| `ISOS_PATH` | Your discs: ISO images, BDMV folders or M2TS files |
| `OUTPUT_PATH` | Where finished MKVs are saved |
| `LIBRARY_PATH` | Your MKV library, to inspect or upgrade existing files *(optional)* |
| `TMP_PATH` | Temporary working space. An SSD is best, with at least twice the size of a film free |
| `CONFIG_PATH` | Where the app keeps its projects and settings |
| `CMV40_RPU_PATH` | An empty folder created for the purpose, unless you keep your own RPU files |

**3. Start the app:**

```bash
docker compose pull
docker compose up -d
```

**4. Open** `http://YOUR-NAS-IP:8090` in the browser.

<details>
<summary><b>With Container Station (QNAP) or Portainer, without the command line</b></summary>

1. Download `docker-compose.yml` and `.env.example` from the links above, rename the second one to `.env` and fill in your folders.
2. Create a *Docker Compose* application (Container Station: *Applications → Create* · Portainer: *Stacks → Add stack*).
3. Paste the contents of `docker-compose.yml`, add the `.env` file and deploy.

</details>

**Updating** takes one command, and the app itself shows a notice in its header when a new version is out:

```bash
docker compose pull && docker compose up -d
```

> [!TIP]
> Film and TV show details from TMDb (posters, synopses, episodes) work right after installation. Searching the DoviTools repository for CMv4.0 files also needs a free Google API key, which goes in ⚙︎ Settings. The built-in manual explains how to get one, step by step.

---

## Your first film

1. **Put a UHD Blu-ray ISO** (or a BDMV folder) in your discs folder.
2. **Rip it.** In **Blu-ray ISO → MKV**, click **New ISO → MKV project**, choose **Movie**, select the ISO and click **Analyze**. Review the tracks the app picked and click **Confirm and run**.
3. **Upgrade it.** In **Dolby Vision CMv4.0 upgrade**, click **New CMv4.0 project**, select the MKV you just made and the file the DoviTools repository suggests, and click **Create project**. Read the recommendation: the app tells you whether the upgrade is worth it.
4. **Add a dub** *(optional)*. In **Multiplex MKVs**, click **New multiplex**, select the upgraded MKV and the release with the audio you want, and choose what comes from each.
5. **See the result.** Open the final MKV in **Inspect / Edit MKV**.

---

## FAQ

<details>
<summary><b>Is anything re-encoded?</b></summary>

No. Video and audio are copied bit for bit in every step. The upgrade changes the Dolby Vision metadata that travels with the video, and the multiplex can change the timing of each frame in the container; the picture itself is never touched.

</details>

<details>
<summary><b>Are my original files modified?</b></summary>

No. Your discs folder and your library are mounted read-only. Every MKV the app produces goes to the output folder, and an edit to a file in your library is made on a copy there.

</details>

<details>
<summary><b>My player already converts to CMv4.0 on the fly. Do I need the upgrade?</b></summary>

It depends on the file, and the app measures it for you. A player that converts on the fly (p3i T4, avdvplus, recent Sony or LG models) builds CMv4.0 metadata from the disc's own, but it cannot add trims a colorist made by hand: those only exist in the studio's CMv4.0 master. When the file carries a colorist's trims, the upgrade puts them in your MKV. When it carries none, the app recommends keeping your MKV, because the result on screen would be the same. When it only carries analysis metadata, the app tells you what you would gain and leaves the decision to you.

</details>

<details>
<summary><b>Where do the CMv4.0 files come from?</b></summary>

Mostly from the DoviTools repository, maintained by R3S3T_9999 (see [Acknowledgements](#acknowledgements)). The app reads its sheet and searches its repository for you; listing the repository needs a free Google API key. You can also take the CMv4.0 metadata from another MKV that already has it, or use your own files.

</details>

<details>
<summary><b>Why does the container run in privileged mode?</b></summary>

To read ISO images without copying them. The app mounts each ISO read-only, and Docker only allows that in privileged mode.

</details>

<details>
<summary><b>How long does it take?</b></summary>

Measured on the author's NAS, a QNAP with a 4-core Celeron N5095 and hard disks:

| Job | Typical time |
|---|---|
| Ripping a UHD film | 10–40 minutes |
| Ripping a TV episode | about 6 minutes |
| A CMv4.0 upgrade | 30–60 minutes |
| A multiplex | about 8 minutes |
| The extended analysis of an MKV | about 7 minutes |

The disks set the pace, not the processor.

</details>

<details>
<summary><b>What happens if a job fails, or the NAS restarts?</b></summary>

A file that fails its checks keeps its temporary name, so it cannot be mistaken for a finished one, and the project keeps its state, its error and its log so the step can be run again. If the container restarts, the queue resumes, and a job that was cut off is marked as interrupted, with its half-written file removed.

</details>

---

## Technical documentation

The full description is in **[docs/TECHNICAL.md](docs/TECHNICAL.md)**. It covers every configuration variable, the Docker volumes, each tool's pipeline phase by phase, the API and local development.

---

## Acknowledgements

UHD Blu-ray Toolkit is built on the work of many people who share it openly.

- **[R3S3t9999 (REC_9999)](https://github.com/R3S3t9999/DoVi_Scripts)** is the author of DoVi_Scripts, the DoviTools sheet and the repository of CMv4.0 files that the upgrade relies on. The repository is maintained by one person and is funded by donations. If you find it useful, you can **[support it on PayPal](https://www.paypal.com/donate/?hosted_button_id=6ML5KUZG9XGB6)**.
- **[dovi_tool](https://github.com/quietvoid/dovi_tool)** by quietvoid analyses, edits and injects the Dolby Vision metadata.
- **[MKVToolNix](https://mkvtoolnix.download/)** by Moritz Bunkus reads the discs and creates and edits the MKVs.
- **[FFmpeg](https://ffmpeg.org/)**, through the builds of **[BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds)**.
- **[MediaInfo](https://mediaarea.net/en/MediaInfo)** by MediaArea provides the technical details of every track.
- **The community of the [Dolby Vision thread on the MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?t=18602)** has documented almost everything there is to know about the subject.
- **[FastAPI](https://fastapi.tiangolo.com/)** and **[Sortable.js](https://sortablejs.github.io/Sortable/)**.

<a href="https://www.themoviedb.org/"><img src="app/static/img/tmdb.svg" alt="The Movie Database (TMDB)" height="18"></a>

Film details, posters and episodes come from **[The Movie Database](https://www.themoviedb.org/)**.
This product uses the TMDB API but is not endorsed or certified by TMDB.

---

## License

The app's code is free software under the **MIT** license ([`LICENSE`](LICENSE)). The Docker image also contains third-party tools under their own licenses, some of them GPL. Their inventory, notices and source code offer are in [`THIRD-PARTY-NOTICES.md`](THIRD-PARTY-NOTICES.md), and also inside the app, under ⚙︎ Settings → About.

---

> [!NOTE]
> UHD Blu-ray Toolkit is meant for backups of discs you own. It does not support material you do not own.
