# Optical Disc Preservation & Access Toolkit

**Professional-grade utilities for optical disc archiving on macOS**

This repository contains two Python scripts for a complete optical disc digitization workflow:

| Script | Purpose |
|--------|---------|
| **`makeiso.py`** | Creates bit-perfect ISO preservation masters from physical discs |
| **`makeiso-video.py`** | Converts DVD-Video ISOs to MP4 access copies |

Part of the Johnson Publishing Company Archive (JPCA) digitization initiative—a collaboration between the [Getty Research Institute](https://www.getty.edu/projects/johnson-publishing-company-archive/) and the [Smithsonian National Museum of African American History and Culture](https://www.searchablemuseum.com/johnson-publishing-company-image-power/).

---

## Table of Contents

- [Overview](#overview)
  - [Workflow Summary](#workflow-summary)
- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
  - [Step 1: Install Homebrew](#step-1-install-homebrew-if-not-already-installed)
  - [Step 2: Install Python 3](#step-2-install-python-3-if-not-already-installed)
  - [Step 3: Set Up a Virtual Environment](#step-3-set-up-a-python-virtual-environment-recommended)
  - [Step 4: Clone the Repository](#step-4-clone-the-repository)
  - [Step 5: Install Required Tools](#step-5-install-required-tools)
  - [Step 6: Verify Setup](#step-6-verify-everything-is-ready)
- [makeiso.py — Preservation Master Creation](#makeisopy--preservation-master-creation)
  - [Quick Start](#quick-start)
  - [Usage](#usage)
  - [Workflow](#workflow)
  - [Output Files](#output-files)
  - [Understanding the Output](#understanding-the-output)
- [makeiso-video.py — Access Copy Creation](#makeiso-videopy--access-copy-creation)
  - [Quick Start](#quick-start-1)
  - [Usage](#usage-1)
  - [Disc Type Handling](#disc-type-handling)
  - [Encoding Defaults](#encoding-defaults)
  - [Output Files](#output-files-1)
- [Troubleshooting](#troubleshooting)
- [Technical Details](#technical-details)
  - [How Verification Works](#how-verification-works)
  - [Manifest Structure](#manifest-structure)
  - [Supported Media Types](#supported-media-types)
- [License](#license)
- [Contributing](#contributing)

---

## Overview

This toolkit provides a two-stage workflow for optical disc digitization:

1. **Preservation** (`makeiso.py`) — Create archival ISO images with comprehensive verification and metadata
2. **Access** (`makeiso-video.py`) — Generate MP4 viewing copies from DVD-Video content

### Workflow Summary

```
┌─────────────────────────────────────────────────────────────────┐
│                    PRESERVATION WORKFLOW                         │
├─────────────────────────────────────────────────────────────────┤
│  Physical Disc  →  makeiso.py  →  .iso (preservation master)    │
│                                    + manifest, log, tree         │
├─────────────────────────────────────────────────────────────────┤
│                      ACCESS WORKFLOW                             │
├─────────────────────────────────────────────────────────────────┤
│  .iso file  →  makeiso-video.py  →  .mp4 (access copy)          │
│                                       + manifest, log            │
│                                                                  │
│     ~75-80% of discs: DVD-Video → MP4 conversion                │
│     ~20-25% of discs: Skipped (data/audio/other)                │
└─────────────────────────────────────────────────────────────────┘
```

---

## Features

### makeiso.py (Preservation)

- **Bit-perfect copying** — Reads from raw device (`/dev/rdiskN`) for true sector-by-sector backup
- **Independent verification** — source hashed in-stream during creation, then the written ISO is re-read from disk and compared
- **Real-time progress** — Live display of speed, elapsed time, and ETA
- **Comprehensive logging** — Detailed operation log with timestamps
- **JSON manifest** — Machine-readable metadata for archival systems
- **Directory tree capture** — Complete file listing from source disc
- **ISO structural analysis** — Validates ISO 9660/UDF structure via isolyzer
- **Automatic disc handling** — Unmounts before backup, ejects after

### makeiso-video.py (Access)

- **Automatic content detection** — Analyzes mounted ISO to detect VIDEO_TS folders
- **Intelligent skipping** — Gracefully handles non-video discs (data DVDs, photo discs, DVD-Audio)
- **High-quality output** — Default CRF 18 produces visually lossless MP4 files
- **Batch processing** — Process entire directories of ISOs with a single command
- **Checksum verification** — MD5 hash of output for integrity verification
- **Comprehensive logging** — Detailed logs and JSON manifests matching preservation conventions

---

## Requirements

### System
- **macOS** (uses `diskutil` and `hdiutil` for disc/image management)
- **Python 3.9+** (uses dataclasses, f-strings, type hints)
- **sudo privileges** (required for raw device access in `makeiso.py`)

### Dependencies

**Included with macOS (no action needed):**
- `diskutil` — Disc management
- `hdiutil` — ISO mounting
- `dd` — Raw device copying

**Installed during setup (see [Installation](#installation)):**
- `tree` — Generates directory listings
- `isolyzer` — ISO structural analysis (optional but recommended)
- `ffmpeg` — Video conversion (required for `makeiso-video.py`)

---

## Installation

### Step 1: Install Homebrew (if not already installed)

Homebrew is a package manager for macOS that makes installing software easy.

Open **Terminal** (Applications → Utilities → Terminal).

**First, check if Homebrew is already installed:**
```bash
brew --version
```

If you see a version number (e.g., `Homebrew 4.x.x`), skip to Step 2.

If you see `command not found: brew`, install Homebrew by pasting:

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

Follow the prompts. The installation will:
- Ask for your Mac password (this is `sudo` access—Homebrew needs administrator privileges to install itself)
- Download and install the Xcode Command Line Tools if not already present
- Take 5–15 minutes depending on your internet connection

You may be prompted for your password multiple times during installation. When complete, close and reopen Terminal (or open a new Terminal tab) for the changes to take effect.

### Step 2: Install Python 3 (if not already installed)

**First, check if Python 3 is already installed:**
```bash
python3 --version
```

If you see `Python 3.9.x` or higher, skip to Step 3.

If you see `command not found` or a version older than 3.9, install Python:

```bash
brew install python
```

> **How does this work?** Running `brew install python` automatically installs the latest stable version of Python 3 that Homebrew supports. It will always install a recent, maintained version.

Verify the installation:

```bash
python3 --version
```

You should see `Python 3.9.x` or higher.

### Step 3: Set Up a Python Virtual Environment (Recommended)

A virtual environment keeps this project's dependencies isolated from other Python projects.

```bash
# Navigate to your home directory
cd ~

# Create a virtual environment named 'venv'
python3 -m venv venv
```

**Understanding the command `python3 -m venv venv`:**

| Part | Meaning |
|------|---------|
| `python3` | Calls the Python 3 interpreter on macOS |
| `-m` | Flag that tells Python to run a module as a script |
| `venv` (first) | The name of Python's built-in virtual environment module |
| `venv` (second) | The name you're giving to your virtual environment folder (can be anything) |

**What just happened?** Running this command creates a new folder called `venv` inside your home directory. This folder contains:

```
~/
├── Documents/
├── Downloads/
└── venv/                  ← Created by the command above
    ├── bin/               ← Contains activation scripts and Python executables
    │   ├── activate       ← The script you'll run to "turn on" the environment
    │   ├── python         ← A copy of Python for this environment
    │   └── pip            ← Package installer for this environment
    ├── lib/               ← Where installed packages (like isolyzer) will live
    └── include/           ← Header files (you can ignore this)
```

**Activating the virtual environment:**

```bash
# Activate the virtual environment
source ~/venv/bin/activate
```

This command runs the `activate` script located inside the `venv/bin/` folder that was just created. The `source` command tells your terminal to run the script in the current session (rather than in a separate process).

> **Note:** When the virtual environment is active, you'll see `(venv)` at the beginning of your terminal prompt.
>
> To deactivate later, simply type: `deactivate`

> **Customizing the environment name:** You can name your virtual environment anything you like. Just replace the second `venv` with your preferred name:
> ```bash
> # Create with a custom name (from your home directory)
> python3 -m venv JPCA-AV
> ```
> This creates a folder called `JPCA-AV` instead of `venv` in your home directory.
> 
> When activating, use the full path:
> ```bash
> # Activate using full path (works from anywhere)
> source ~/JPCA-AV/bin/activate
> ```
> Your prompt will then show `(JPCA-AV)` instead of `(venv)`.

> **Tip:** You can add an alias to your shell profile (`~/.zshrc` on modern macOS) to make activation easier:
> ```bash
> alias jpca="source ~/JPCA-AV/bin/activate"
> ```
> Then simply type `jpca` to activate the environment from anywhere.

### Step 4: Clone the Repository

Clone the project repository to wherever you keep your code. Common locations include your home directory (`~`) or a dedicated folder like `~/github/`:

```bash
# Option A: Clone to home directory
cd ~
git clone https://github.com/JPC-AV/optical_disc_jpc_av.git

# Option B: Clone to a github folder (create it first if needed)
mkdir -p ~/github
cd ~/github
git clone https://github.com/JPC-AV/optical_disc_jpc_av.git
```

Then enter the project folder:

```bash
cd optical_disc_jpc_av
```

> **Note:** This is where the *scripts* live, not where your backups will be saved. When you run the scripts, they will ask you to specify an output directory—that can be anywhere you like (e.g., an external drive, a NAS, `~/Backups/`, etc.).

### Step 5: Install Required Tools

With your virtual environment activated, install the additional tools:

**Install tree** (using Homebrew):
```bash
brew install tree
```
`tree` generates directory listings of the disc contents before backup.

**Install ffmpeg** (using Homebrew):
```bash
brew install ffmpeg
```
`ffmpeg` is required for `makeiso-video.py` to convert VIDEO_TS content to MP4.

**Install isolyzer** (using pip):
```bash
pip install isolyzer
```

> **What is pip?** `pip` is Python's package installer—it downloads and installs Python libraries from the internet. It was automatically installed when you installed Python 3 in Step 2. When your virtual environment is active, `pip install` puts packages into your `venv/lib/` folder, keeping them isolated from other projects.

> **Note:** `isolyzer` is optional but recommended. It validates the structure of the created ISO file. `makeiso.py` will work without it, but ISO structural validation will be skipped.

### Step 6: Verify Everything is Ready

```bash
# Check Python
python3 --version

# Check tree
tree --version

# Check ffmpeg
ffmpeg -version

# Check isolyzer
isolyzer --version
```

If all commands return version information, you're ready to go!

---

## makeiso.py — Preservation Master Creation

Creates archival-quality ISO images from optical discs while simultaneously verifying data integrity. Designed for digital preservation workflows where verification and documentation are critical.

The script reads directly from the raw device (bypassing filesystem caching) and calculates the source MD5 and SHA-256 checksums during the copy. After the copy completes, it verifies the backup by re-reading the written ISO from disk and comparing both hashes against the source — so verification reflects the bytes that actually landed on disk, not the in-memory stream.

### Quick Start

1. Insert an optical disc
2. Activate your virtual environment:
   ```bash
   source ~/venv/bin/activate
   ```
3. Navigate to the project folder:
   ```bash
   cd ~/github/optical_disc_jpc_av
   ```
4. Run the script with sudo:
   ```bash
   sudo python3 makeiso.py
   ```
5. Follow the prompts to select the disc and specify output location
6. Wait for the backup to complete
7. Find your ISO and documentation files in the output directory

> **Why `sudo`?** The script needs administrator privileges to read directly from the optical drive hardware. You'll be prompted for your Mac password.

### Usage

#### Interactive Mode

Running without arguments launches interactive mode, which prompts for all required information:

```bash
sudo python3 makeiso.py
```

The script will:
1. Display all connected disks
2. Prompt for the disc ID (e.g., `disk2`)
3. Auto-detect the volume name
4. Prompt for output filename and directory
5. Prompt for operator name/initials

#### Command Line Options

```
sudo python3 makeiso.py [options]

Options:
  -h, --help              Show help message and exit
  --dry-run               Run without creating ISO (test mode)
  --force                 Skip the optical-media safety check (allows imaging devices diskutil does not report as optical)
  --filename NAME         ISO filename (without .iso extension)
  --dir PATH              Output directory (supports ~)
  --operator NAME         Operator name or initials for logging
```

#### Examples

**Fully interactive:**
```bash
sudo python3 makeiso.py
```

**Specify all options (still prompts for disk ID):**
```bash
sudo python3 makeiso.py --filename "My_DVD_Backup" --dir ~/Backups --operator JD
```

**Test run without creating files:**
```bash
sudo python3 makeiso.py --dry-run
```

**Quick backup with operator initials:**
```bash
sudo python3 makeiso.py --operator ABC --dir ~/ISO_Archive
```

### Workflow

The script executes the following steps:

```
┌─────────────────────────────────────────────────────────────────┐
│  1. INITIALIZATION                                              │
│     • Display available disks                                   │
│     • Collect user inputs (disk ID, filename, etc.)             │
│     • Validate disk exists and is optical media                 │
└─────────────────────────────────────────────────────────────────┘
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  2. TREE LISTING                                                │
│     • Generate directory structure of mounted volume            │
│     • Save to {filename}_tree.txt                               │
└─────────────────────────────────────────────────────────────────┘
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  3. UNMOUNT                                                     │
│     • Safely unmount disc (required for raw access)             │
└─────────────────────────────────────────────────────────────────┘
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  4. ISO CREATION + VERIFICATION (two-phase)                     │
│     • Read from raw device (/dev/rdiskN)                        │
│     • Write to ISO file                                         │
│     • Calculate MD5 + SHA-256 of source stream                  │
│     • Flush to disk, re-read ISO, verify hashes                 │
│     • Display real-time progress                                │
└─────────────────────────────────────────────────────────────────┘
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  5. STRUCTURAL ANALYSIS                                         │
│     • Run isolyzer on created ISO                               │
│     • Validate ISO 9660 / UDF structure                         │
│     • Save raw XML to {filename}_isolyzer.xml                   │
└─────────────────────────────────────────────────────────────────┘
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  6. FINALIZATION                                                │
│     • Eject disc                                                │
│     • Generate formatted log file                               │
│     • Generate JSON manifest                                    │
│     • Display summary                                           │
└─────────────────────────────────────────────────────────────────┘
```

### Output Files

For a backup with filename `JPC_AV_00001`, the script creates a directory containing:

```
~/Backups/JPC_AV_00001/
├── JPC_AV_00001.iso              # The ISO image (preservation master)
├── JPC_AV_00001.iso.log.txt      # Human-readable operation log
├── JPC_AV_00001_manifest.json    # Machine-readable metadata
├── JPC_AV_00001_tree.txt         # Directory listing of source disc
└── JPC_AV_00001_isolyzer.xml     # Raw ISO structural analysis
```

| File | Purpose |
|------|---------|
| `*.iso` | Bit-perfect disc image, mountable on any system |
| `*.iso.log.txt` | Detailed log with timestamps, commands run, checksums, and verification results |
| `*_manifest.json` | Comprehensive JSON metadata for archival systems and databases |
| `*_tree.txt` | Complete directory listing with file sizes, permissions, and dates |
| `*_isolyzer.xml` | ISO 9660/UDF structural validation from isolyzer tool |

#### Failed Run Naming

Failed or aborted runs are renamed so they can never be mistaken for finished masters. All artifacts from a failed attempt share a `failed-<timestamp>-<random>` token, and retries never overwrite them:

```
JPC_AV_00001_failed-20260612T140322-3f9a2c.iso.partial    # copy aborted partway
JPC_AV_00001_failed-20260612T140322-3f9a2c.iso.mismatch   # completed but failed verification
JPC_AV_00001_failed-20260612T140322-3f9a2c.iso.log.txt    # log for that attempt
JPC_AV_00001_failed-20260612T140322-3f9a2c_manifest.json  # manifest for that attempt
JPC_AV_00001_failed-20260612T140322-3f9a2c_tree.txt       # tree listing for that attempt
JPC_AV_00001_failed-20260612T140322-3f9a2c_isolyzer.xml   # structural analysis, if it ran
```

Successful runs use the standard names above.

### Understanding the Output

#### Terminal Output

During execution, you'll see color-coded status updates:

- 🔵 **Blue** — Section headers
- 🔷 **Cyan** — Information and commands being run
- 🟡 **Yellow** — File paths and values
- 🟢 **Green** — Success messages
- 🔴 **Red** — Errors

#### Progress Display

```
ISO Creation: 2048MB / 4096MB
Elapsed: 0:01:23
Remaining: 0:01:24
Avg Speed: 24.67MB/s
```

#### Verification Output

```
MD5 (ISO):          a1b2c3d4e5f6...
MD5 (Raw Disk):     a1b2c3d4e5f6...
SHA-256 (ISO):      9f8e7d6c5b4a...
SHA-256 (Raw Disk): 9f8e7d6c5b4a...
Checksum match: ISO on disk is a true bit-for-bit copy.
```

#### Exit Codes

| Code | Meaning |
|------|---------|
| `0` | Success — backup completed (see note below) |
| `1` | Failure — backup failed (see error message) |
| `2` | Verification failed — written ISO does not match the source (run is marked failed) |

> **Note:** Exit `0` covers three record states — the manifest's `overall_status` and the log header are the source of truth:
> - `success` — verified master produced
> - `success_with_warnings` — verified master, with a non-fatal problem recorded (e.g. a failed eject or an incomplete tree listing; this workflow is operator-attended, so these don't change the exit code)
> - `dry_run` — `--dry-run` rehearsal; no ISO was created and nothing was verified (log header reads `DRY RUN`)

---

## makeiso-video.py — Access Copy Creation

Converts DVD-Video content from ISO preservation files to MP4 access copies. Designed to work with the output from `makeiso.py` and handles the common case where ~75-80% of archived optical discs contain standard VIDEO_TS DVD-Video structures.

### Quick Start

1. Activate your virtual environment:
   ```bash
   source ~/venv/bin/activate
   ```
2. Navigate to the project folder:
   ```bash
   cd ~/github/optical_disc_jpc_av
   ```
3. Run the script:
   ```bash
   python3 makeiso-video.py -i /path/to/JPC_AV_00001.iso --operator JD
   ```
4. Find your MP4 in `access_JPC_AV_00001/` alongside the original ISO

> **Note:** Unlike `makeiso.py`, this script does **not** require `sudo` because it reads from ISO files rather than raw hardware devices.

### Usage

#### Running with No Arguments

Running with no arguments displays a brief usage message:

```bash
python3 makeiso-video.py
```

```
MAKEISO-VIDEO - ISO TO MP4 ACCESS COPY UTILITY

Usage:
  python3 makeiso-video.py -i <iso_file> [options]
  python3 makeiso-video.py --batch <directory> [options]

Required (one of):
  -i, --iso PATH    Path to ISO file
  --batch PATH      Process all ISOs in directory

Run python3 makeiso-video.py --help for full options.
```

#### Command Line Options

```
python3 makeiso-video.py [options]

Required (one of):
  -i, --iso PATH          Path to ISO file
  --batch PATH            Process all ISOs in directory

Output Options:
  -o, --output PATH       Output directory (default: same directory as ISO)
  --operator NAME         Operator name or initials

Encoding Options:
  --crf N                 Quality (0-51, default: 18 = visually lossless)
                          CRF stands for Constant Rate Factor
  
  --preset NAME           Encoding speed (default: medium)
                          ultrafast/fast/medium/slow/veryslow
  
  --audio-bitrate RATE    Audio bitrate (default: 192k)

  CRF vs Preset:

  CRF controls the target quality level. It tells the encoder "make the output
  look this good" and lets the file size be whatever it needs to be to achieve
  that quality. Lower CRF = higher quality = larger file.

  Preset controls the encoding efficiency (how hard the encoder works to
  compress). At the same CRF, a slower preset will produce a smaller file
  because it spends more CPU time finding better compression opportunities.
  A faster preset produces a larger file at the same visual quality because
  it takes shortcuts.

  Think of it this way: CRF sets the destination (quality), preset determines
  how efficiently you get there (time vs. file size tradeoff).

  Example: CRF 18 with --preset ultrafast might produce a 2GB file in 10
  minutes. CRF 18 with --preset veryslow might produce a 1.4GB file in 2 hours.
  Both look identical—the slow one just found more compression opportunities.

Other Options:
  -h, --help              Show help message
  -n, --dry-run           Analyze without converting
```

#### Examples

**Convert single ISO (output to same directory):**
```bash
python3 makeiso-video.py -i /path/to/JPC_AV_00001.iso --operator JD
# Creates: /path/to/access_JPC_AV_00001/JPC_AV_00001.mp4
```

**Convert with custom output location:**
```bash
python3 makeiso-video.py -i disc.iso -o /path/to/access_copies --operator JD
```

**Batch convert all ISOs in a directory:**
```bash
python3 makeiso-video.py --batch /path/to/isos --operator JD
```

**Preview without converting (dry run):**
```bash
python3 makeiso-video.py -i disc.iso -n
```

**Higher quality, slower encoding:**
```bash
python3 makeiso-video.py -i disc.iso --crf 16 --preset slow --operator JD
```

### Disc Type Handling

The script automatically detects disc content and handles different types appropriately:

| Disc Type | Action | Notes |
|-----------|--------|-------|
| DVD-Video (VIDEO_TS) | ✅ Converted | One MP4 per titleset (see below) |
| Menu-only VIDEO_TS | ⏭️ Skipped | No content VOBs — nothing to convert |
| DVD-Audio (AUDIO_TS) | ⏭️ Skipped | Requires specialized audio extraction |
| Data DVD | ⏭️ Skipped | Use file copy instead |
| Photo/Image DVD | ⏭️ Skipped | Use file copy instead |
| Empty/Unreadable | ⏭️ Skipped | Verify ISO integrity |

Skipped discs generate a `*_skip_manifest.json` documenting why the disc was skipped.

### Titlesets: One MP4 Per Titleset

DVDs group content into **titlesets** (`VTS_01_*`, `VTS_02_*`, …) — separate programs such as a main feature plus extras, or multiple episodes. The script encodes **each titleset to its own MP4**:

- The lowest-numbered content titleset is the *main* title and keeps the plain name: `JPC_AV_00001.mp4`
- Additional titlesets get suffixes matching their VTS number: `JPC_AV_00001_title02.mp4`, etc.
- Single-titleset discs (the vast majority of the collection) produce exactly one MP4 with the same name as before.
- The manifest's `titlesets` section records every output with its own size, duration, and checksum.

> **Scope note:** grouping is *titleset-aware*, not title-aware — a single titleset can still contain multiple programs/PGCs/angles internally. This is not DVD-player-equivalent title parsing. The preservation ISO always retains the complete original structure.

### Stream Handling

Each encode uses explicit stream mapping (`-map 0:v:0 -map 0:a? -sn`):

- **All audio tracks** on the disc are carried into the MP4 as selectable tracks (e.g. narration and ambient audio both survive).
- **Subtitles are not encoded** — DVD bitmap subtitles are not MP4-compatible; their presence is recorded in the manifest, and they remain intact in the preservation ISO.
- The manifest records both the source stream counts and the streams actually mapped into each output, plus a best-effort duration comparison that warns if an output is >5% shorter than expected (possible truncation).

### Failed Conversion Naming

Encodes write to a temporary `*.encoding-<random>.mp4` file that atomically replaces the final name only after the output verifies — a failed retry can never damage a pre-existing good MP4. All artifacts of one failed attempt share a `failed-<timestamp>-<random>` token (`aborted-…` for operator aborts) so retries never overwrite earlier evidence:

```
JPC_AV_00001_failed-20260612T150000-3f9a2c.mp4.partial   # quarantined partial encode
JPC_AV_00001_failed-20260612T150000-3f9a2c.log.txt       # log for that attempt
JPC_AV_00001_failed-20260612T150000-3f9a2c_manifest.json # manifest for that attempt
```

A hard kill (power loss, `kill -9`) may leave a `*.encoding-*.mp4` temp file behind; it is incomplete by definition and safe to delete.

### Menu VOBs Are Intentionally Excluded

> **Note:** During conversion you will see output like:
> ```
> Skipping 1 menu VOB(s):
>   - VIDEO_TS.VOB (menu VOB, 8.0 KB)
> Including 2 content VOB(s):
>   + VTS_01_1.VOB (1024.0 MB)
>   + VTS_01_2.VOB (833.2 MB)
> ```
> This is expected and correct behavior — not an error.

DVD-Video discs contain two categories of VOB files:

- **Menu VOBs** (`VIDEO_TS.VOB`, `VTS_*_0.VOB`) — contain interactive navigation data: button layouts, highlight graphics, menu audio loops, and DVD player commands. This data is designed to be driven by a DVD player's menu system and cannot be included in a linear MP4 stream. Attempting to include it causes ffmpeg errors or corrupted output.

- **Content VOBs** (`VTS_*_1.VOB`, `VTS_*_2.VOB`, etc.) — contain the actual linear video content: everything you would see by pressing Play All on the disc.

The script automatically skips menu VOBs and concatenates only the content VOBs, per titleset. A disc whose VIDEO_TS contains *only* menu VOBs is skipped (nothing to convert). **No video content is lost.** The full disc — including all menu structure, navigation data, logos, and interactive elements — remains intact in the preservation ISO, which can be mounted on any computer to experience the disc exactly as originally authored, menus and all.

### Encoding Defaults

The default encoding settings are optimized for archival access copies:

| Setting | Default Value | Description |
|---------|---------------|-------------|
| **Video Codec** | libx264 (H.264) | Industry-standard, excellent compatibility |
| **CRF Quality** | 18 | Visually lossless quality |
| **Preset** | medium | Balanced speed/compression |
| **Pixel Format** | yuv420p | Maximum compatibility |
| **Audio Codec** | AAC | Standard audio codec |
| **Audio Bitrate** | 192 kbps | High quality audio, per track |
| **Stream Mapping** | `-map 0:v:0 -map 0:a? -sn` | First video, all audio tracks, no subtitles |
| **Container** | MP4 | Universal playback support |
| **Faststart** | enabled | Enables streaming/seeking |

#### CRF Quality Guide

| CRF Value | Quality Level | Typical Use Case |
|-----------|---------------|------------------|
| 0 | Lossless | Archival masters (huge files) |
| 16 | Near-lossless | High-end preservation |
| **18** | **Visually lossless** | **Default — recommended for access** |
| 23 | Good quality | General purpose |
| 28 | Acceptable | Space-constrained |

#### Preset Speed/Size Tradeoff

| Preset | Speed | File Size | Use Case |
|--------|-------|-----------|----------|
| ultrafast | Fastest | Largest | Quick previews |
| fast | Fast | Larger | Time-sensitive |
| **medium** | **Balanced** | **Balanced** | **Default** |
| slow | Slow | Smaller | Best compression |
| veryslow | Slowest | Smallest | Maximum compression |

### Output Files

For a successfully converted ISO, the script creates:

```
/path/to/isos/
├── JPC_AV_00001.iso                        # Original preservation ISO
├── JPC_AV_00001/                           # Preservation metadata folder
│   ├── JPC_AV_00001_manifest.json
│   └── ...
└── access_JPC_AV_00001/                    # Access copy folder
    ├── JPC_AV_00001.mp4                    # The access copy
    ├── JPC_AV_00001.mp4.log.txt            # Detailed conversion log
    └── JPC_AV_00001_access_manifest.json   # JSON metadata
```

For skipped ISOs (non-VIDEO_TS content):

```
/path/to/isos/
├── data_disc.iso
└── access_JPC_AV_data_disc/
    └── data_disc_skip_manifest.json        # Documents why disc was skipped
```

---

## Troubleshooting

### General Issues

#### "This script must be run with sudo"

Raw device access requires root privileges (for `makeiso.py` only):
```bash
sudo python3 makeiso.py
```

#### "Could not get disk info for diskN"

- Verify the disc is inserted and recognized
- Check disk ID with `diskutil list`
- Ensure you're using the correct disk identifier (e.g., `disk2`, not `disk2s1`)

### makeiso.py Issues

#### "isolyzer not found"

Install isolyzer for ISO structural analysis:
```bash
pip install isolyzer
```
The script will still work without it, but structural validation will be skipped.

#### "tree: command not found"

Install tree for directory listings:
```bash
brew install tree
```

#### Slow backup speed

- External USB 2.0 drives are limited to ~30 MB/s
- USB 3.0 or Thunderbolt drives will be faster
- Damaged discs may cause slowdowns due to read retries

#### Checksum mismatch

This indicates the ISO is **not** identical to the source disc. Possible causes:
- Disc read errors (scratches, degradation)
- Drive malfunction
- System memory issues

**Recommendation:** Re-run the backup. If mismatches persist, inspect the disc for physical damage.

### makeiso-video.py Issues

#### "ffmpeg not found"

Install ffmpeg using Homebrew:
```bash
brew install ffmpeg
```

#### ISO won't mount

1. Check ISO integrity: `hdiutil verify /path/to/disc.iso`
2. Try mounting manually: `hdiutil attach /path/to/disc.iso`
3. Verify the ISO was created correctly with `makeiso.py`

#### Conversion fails partway through

1. Check available disk space
2. Verify ISO integrity
3. Try with `-n` (dry run) first to analyze content
4. Check the log file for detailed error messages

#### Poor video quality

Adjust encoding settings:
```bash
# Lower CRF = higher quality (and larger files)
python3 makeiso-video.py -i disc.iso --crf 16

# Slower preset = better compression at same quality
python3 makeiso-video.py -i disc.iso --preset slow
```

---

## Technical Details

### How Verification Works

`makeiso.py` verifies in **two phases**, so the two hashes being compared come from independent reads:

```
Phase 1 — Creation (single pass over the disc)

┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│  Raw Device  │────▶│  4MB Chunk   │────▶│   ISO File   │
│ /dev/rdisk2  │     │              │     │              │
└──────────────┘     └──────┬───────┘     └──────────────┘
                            │
                            ▼
                     ┌────────────┐
                     │  MD5 Hash  │
                     │   (raw)    │
                     └────────────┘

Phase 2 — Verification (after forcing the file to physical disk)

┌──────────────┐     ┌──────────────┐     ┌────────────┐
│   ISO File   │────▶│  4MB Chunk   │────▶│  MD5 Hash  │
│  re-read,    │     │              │     │   (iso)    │
│ cache bypass │     └──────────────┘     └────────────┘
└──────────────┘
```

During creation, each 4MB chunk is read from the raw device, written to the ISO file, and fed to the source (raw) hashers — both MD5 and SHA-256. After the copy completes, the script forces the file to physical media (`F_FULLFSYNC`), then re-reads the written ISO from disk with the page cache bypassed (`F_NOCACHE`) and hashes it independently with both algorithms. A match requires both digest pairs to agree; MD5 is retained for continuity with existing collection manifests, SHA-256 for current archival fixity practice.

Because the second hash comes from the bytes that actually landed on disk — not from the in-memory stream — a mismatch catches write errors, truncated writes, and destination-disk corruption. Verification always runs; there is no option to skip it.

### Manifest Structure

The JSON manifest (`*_manifest.json`) includes:

- `backup_metadata` — Run ID, UUID, timestamps, operator
- `backup_status` — Success/failure, verification results
- `source_disc` — Device info, media type, filesystem
- `volume_information` — Size, mount point, block size
- `output_files` — All generated file paths and sizes
- `operations_performed` — Commands run with status
- `timing_performance` — Duration and speed metrics
- `integrity_verification` — Hash values and match status
- `structural_analysis` — isolyzer results with XML field mappings
- `system_environment` — OS, Python version, hostname
- `quality_assurance` — Verification levels and recommendations

The access manifest (`*_access_manifest.json`) includes similar metadata plus:

- `encoding_settings` — Video/audio codec, CRF, preset
- `disc_analysis` — VIDEO_TS detection results, VOB file inventory
- `video_technical_info` — Resolution, frame rate, duration
- `archival_notes` — Purpose, preservation relationship

### Supported Media Types

- CD-ROM / CD-R / CD-RW
- DVD-ROM / DVD±R / DVD±RW
- BD-ROM / BD-R / BD-RE (Blu-ray)

For `makeiso-video.py`, only DVD-Video discs with VIDEO_TS folders are converted to MP4. Other disc types are detected and skipped with documentation.

---

## License

MIT License — use freely for any purpose, with or without attribution.

This project is part of the Johnson Publishing Company Archive (JPCA) digitization and preservation initiative, a collaboration between the [Getty Research Institute (GRI)](https://www.getty.edu/projects/johnson-publishing-company-archive/) and the [Smithsonian National Museum of African American History and Culture (NMAAHC)](https://www.searchablemuseum.com/johnson-publishing-company-image-power/).

---

## Contributing

Issues and pull requests welcome at:  
https://github.com/JPC-AV/optical_disc_jpc_av

---

*Built by NMAAHC for audiovisual archival preservation workflows supporting the Johnson Publishing Company Archive.*