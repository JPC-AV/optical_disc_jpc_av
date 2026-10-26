# Optical Disc Preservation & Access Toolkit

**Professional-grade utilities for optical disc archiving on macOS**

This repository contains two Python scripts for a complete optical disc digitization workflow:

| Script | Purpose |
|--------|---------|
| **`makeiso.py`** | Creates bit-perfect ISO preservation masters from physical discs |
| **`makeiso-video.py`** | Converts DVD-Video ISOs to MP4 access copies |
| **`verifyiso.py`** | Re-verifies existing ISO masters against their manifest hashes — no discs needed |

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
  - [Duration Check and Outcomes](#duration-check-and-outcomes)
  - [Encoding Defaults](#encoding-defaults)
  - [Output Files](#output-files-1)
- [verifyiso.py — Collection Fixity Verification](#verifyisopy--collection-fixity-verification)
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
- **Checksum recording** — MD5 and SHA-256 hashes of every output for fixity
- **Duration check** — Each MP4 is checked against the disc's expected duration; nothing gets its final name unless every titleset passes
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

The script reads directly from the raw device (bypassing filesystem caching) and calculates the source MD5 and SHA-256 checksums during the copy. After the copy completes, it asks for the ISO to be flushed to its media, then verifies the backup by re-reading the written ISO and comparing both hashes against the source — so verification reflects a second read of the written file, not the in-memory stream. How far that read reaches the media depends on the flush and cache controls the system honoured (see [How Verification Works](#how-verification-works)).

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
  --dry-run               Rehearse without creating an ISO (still writes tree, log and manifest; see Dry Runs)
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

**Rehearsal without creating an ISO** (it still writes a tree listing, log and manifest, so the output folder must not hold this filename's bundle; see [Dry Runs](#dry-runs)):
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
│     • Request flush, re-read ISO, verify hashes                 │
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

#### Replacing an Existing Bundle

If the folder already holds a bundle, you are asked before anything starts. Nothing is touched until the new copy has verified. Then the current bundle is moved, unchanged and under its own file names, into a new folder, and the new bundle is published in its place. Publication requires the verified ISO itself: if it is missing or cannot be inspected when publication starts, nothing is archived or published and the run fails (its files keep `failed-*` names, below); if it disappears after the current bundle was archived, the run fails and the rollback described below applies. (A `--dry-run` never replaces a bundle; see [Dry Runs](#dry-runs).)

```
~/Backups/JPC_AV_00001/
├── JPC_AV_00001.iso                                  # the new master
├── JPC_AV_00001_manifest.json                        # ... and the rest of the new bundle
└── JPC_AV_00001_overwritten-20261024T101500-7b01de/  # the bundle it replaced
    ├── JPC_AV_00001.iso
    ├── JPC_AV_00001_manifest.json
    └── ...
```

Each archived manifest still names its own ISO, so `verifyiso.py` checks every retained master against its own hashes. Inside archived records only the file names are current: their full paths (output directory, final destination) still name the canonical location, which now holds the new master. `makeiso-video.py --batch` skips these folders.

The few renames that archive the old bundle and publish the new one run with Ctrl-C held, so Ctrl-C cannot stop them halfway. If a rename fails, they are rolled back as far as the filesystem allows. The rollback is best effort and never replaces a file. If this run's files cannot all be moved out of the canonical names, nothing is restored. If a restore fails partway, the files already restored are moved back into the archive folder. So after a single failing rename, a bundle that was archived whole stays complete in its archive folder, where each manifest still names its own ISO. If renames keep failing, files can be left split between canonical names and the archive folder. The failed run's records name where each of this run's files actually is, including any the rollback had to leave at a canonical name, and their warnings say where the prior files are; where a file's location could not be checked, the warnings say so instead. A folder the rollback empties is removed.

#### Dry Runs

A `--dry-run` makes no ISO and verifies nothing, but it does write its own tree listing, log and manifest (`overall_status` `dry_run`) under the canonical names. So it is refused before anything is written (exit `1`, no prompt) if any of the five bundle names — `<name>.iso`, `<name>.iso.log.txt`, `<name>_manifest.json`, `<name>_tree.txt`, `<name>_isolyzer.xml` — is already taken in the output folder, by a file (even an empty one), a folder or a symlink (even a broken one). Choose a fresh `--dir` or `--filename`. Other files in the folder are allowed and left alone.

Each dry-run file is created new, never over an existing file or through a symlink; if a name is taken while the run is going, the run stops there and leaves that file alone. A dry run whose tree listing or records fail stops with exit `1` and lists the exact paths of the files it created, which may be incomplete: remove only those if they are not wanted, never the whole folder. A successful dry run's manifest names no existing ISO, so `verifyiso.py` reports it `SKIPPED`.

Under `sudo`, the files a dry run creates are handed to the invoking user through their own open descriptors. An output folder the dry run creates keeps its owner (root), because the run cannot prove that the folder at that path is still the one it made; create the folder yourself first if you want to manage the dry run's files without `sudo`.

#### Failed Run Naming

Failed or aborted runs are renamed so they can never be mistaken for finished masters. All artifacts from a failed attempt share a `failed-<timestamp>-<random>` token, and retries never overwrite them:

```
JPC_AV_00001_failed-20260612T140322-3f9a2c.iso.partial    # copy aborted partway, or a verified copy that could not be published (see its manifest)
JPC_AV_00001_failed-20260612T140322-3f9a2c.iso.mismatch   # completed but failed verification
JPC_AV_00001_failed-20260612T140322-3f9a2c.iso.log.txt    # log for that attempt
JPC_AV_00001_failed-20260612T140322-3f9a2c_manifest.json  # manifest for that attempt
JPC_AV_00001_failed-20260612T140322-3f9a2c_tree.txt       # tree listing for that attempt
JPC_AV_00001_failed-20260612T140322-3f9a2c_isolyzer.xml   # structural analysis, if it ran
```

Successful runs use the standard names above. When a verified run then fails to record or publish, removal of its staged success log and manifest is attempted, at the canonical name if its rollback had to leave the log there. A staged record that cannot be removed is identified in the failed run's records, which say it does not describe a successful run. A file that cannot be given its `failed-*` name keeps its `attempt-*` name (or the canonical name a failed rollback left it at), and the records name it there, with a warning. If this run's ISO or isolyzer output cannot even be checked (an error other than "no such file"), a verified run fails rather than write success records about it, and failure records say what could not be checked and why (the ISO's size as unavailable) instead of claiming the file is absent.

#### Interruptions (Ctrl-C) and Recovery

- **During the tree listing, unmount, copy, verification or structural analysis:** the run is recorded as failed (`Interrupted by operator (Ctrl-C)`), with its files under `failed-*` names; a complete, hash-verified copy interrupted during the analysis is kept as `.iso.partial`.
- **While a failed run's files are renamed to their `failed-*` names:** Ctrl-C is held until those renames finish; the failure and its exit code stand.
- **During the disc eject:** recorded as the warning `disc eject interrupted by operator (Ctrl-C)` on any outcome. After a verified copy the master is kept and published as verified (`success_with_warnings`) and the run exits `130`; if the copy had failed, that failure and its exit code stand.
- **While the run records are written:** recorded as a failed run; nothing is published.
- **During the final renames:** Ctrl-C is held until they finish. If publication completes, the bundle and its records are final and accurate (they do not mention the Ctrl-C) and the run exits `130`. If a rename failed, the best-effort rollback runs first (see [Replacing an Existing Bundle](#replacing-an-existing-bundle)) and the run is recorded as failed.
- **After the run is recorded** (summary, file-ownership fix): a successful run exits `130`, its bundle and records final; file ownership may not have been fixed. A failed run keeps its own exit code.
- **In a `--dry-run`:** Ctrl-C simply stops the run, as before; nothing more is recorded, and the tree listing, log or manifest it had already created may be incomplete (see [Dry Runs](#dry-runs)).

**Only Ctrl-C is handled.** Closing the Terminal window, `kill`, `kill -9` or a power loss stops the script without clean-up, so files may be left under their canonical names, inside an `*_overwritten-*` folder, or under `attempt-*` names. Before changing anything, check each manifest's `run_id` and the `iso_filename` it names, and keep every ISO until the right one is identified. A leftover `*_attempt-*_manifest.json` belongs to an attempt whose publication did not finish; it names the canonical ISO, so if `verifyiso.py` reports `PASS` for it the canonical ISO is that attempt's master, and if `FAIL`, it is not.

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
Checksum match: the re-read ISO matches the stream read from the disc.
```

#### Exit Codes

| Code | Meaning |
|------|---------|
| `0` | Success — backup completed (see note below) |
| `1` | Failure — backup failed (see error message), or a dry run was refused or failed |
| `2` | Verification failed — written ISO does not match the source (run is marked failed) |
| `130` | Ctrl-C was received, but the master had verified and was kept (see [Interruptions](#interruptions-ctrl-c-and-recovery); only an interrupted eject is also recorded, as a warning); a failed run keeps its own code, and a `--dry-run` just stops |

> **Note:** Exit `0` covers three record states — the manifest's `overall_status` and the log header are the source of truth:
> - `success` — verified master produced
> - `success_with_warnings` — verified master, with a non-fatal problem recorded (e.g. a failed eject, an incomplete tree listing, or a weaker verification: `F_FULLFSYNC` unavailable so only `fsync` was used, or `F_NOCACHE` unavailable for the re-read; this workflow is operator-attended, so these don't change the exit code)
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
Batch mode skips superseded masters inside `makeiso.py` archive folders (`*_overwritten-<stamp>-<random>/`, see [Replacing an Existing Bundle](#replacing-an-existing-bundle)) and lists them; convert one explicitly with `-i`, and `-o` pointing elsewhere (otherwise its access folder is written inside the archive folder).

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
| Analysis error (an error while reading the disc's folders, e.g. a VIDEO_TS listing that fails partway) | ❌ Failed | Nothing is planned or encoded, dry runs included; failure records are written and a batch counts it as failed |

A skipped disc gets a log and manifest of its own, `<name>_skipped-<timestamp>-<random>.log.txt` and `<name>_skipped-<timestamp>-<random>_manifest.json`, documenting why it was skipped; a dry run's are named `<name>_dryrun-<timestamp>-<random>…` the same way. Each is created new, so it never replaces another run's records, and it describes only that attempt: a final MP4, log and access manifest beside it come from a conversion. Earlier versions wrote every skip to `<name>_skip_manifest.json` (and every dry-run log to `<name>.mp4.dryrun.log.txt`), each over the last; those files are left in place, and a later successful conversion lists them under `earlier_skip_records` in its access manifest (and in its log) as records of an earlier run.

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
- The manifest records both the source stream counts and the streams actually mapped into each output (null, and "unknown" in the log, when that probe failed or never ran; 0 only when a probe found none), plus each output's duration check (see [Duration Check and Outcomes](#duration-check-and-outcomes)).

### Duration Check and Outcomes

Each titleset is encoded to a temporary file and checked before anything gets a final name:

1. ffmpeg must succeed and leave a non-empty file with a video stream.
2. The MP4's duration must be measurable.
3. It is compared with the titleset's **expected duration**, taken from:
   - the titleset's IFO playback time, but only for one simple, straight-through layout: a single, structurally valid program chain, played in order, whose cells each play once and exactly cover the encoded VOBs — no angles, interleaving, still frames or cell commands — and whose time matches the sum of its cells' (to within a frame per cell). Any other IFO layout is recorded as *advisory* and never decides the outcome;
   - otherwise, the sum of the VOBs' probed durations, but only if every VOB could be measured.

   An expected duration implying more than 12 Mbps over the VOBs is impossible for DVD-Video and is rejected.
4. The tolerance is 5% of the expected duration, but at least 1 second.

| Outcome | When | What is kept |
|---------|------|--------------|
| **passed** | within the tolerance (or exactly on it) | the MP4 — promoted only if every titleset of the ISO passed |
| **unverified** | the check could not be completed: the output's stream or duration probe failed, there is no usable expected duration, or the MP4 is longer than expected by more than the tolerance | a review copy, `*_unverified-<token>.mp4.unverified` |
| **failed** | ffmpeg failed or was interrupted; the output is missing, empty, or confirmed by a successful probe to have no video; or it is shorter than expected by more than the tolerance | the attempt, `*_failed-<token>.mp4.partial`, once ffmpeg has stopped (neither complete nor playable) |

Without a usable expected duration the MP4 is never compared, so it is never reported as truncated on that basis; it is unverified.

**Nothing is promoted unless every titleset passes.** Then all of the ISO's MP4s get their final names and the normal log and manifest are written. Otherwise no final MP4, log or manifest is created or changed: failed titlesets are kept as `.partial`, unverified ones as review copies, and titlesets that passed but were held back because another did not are kept as review copies too, with the reason recorded. That run's log and manifest go under its token (see [Failed Conversion Naming](#failed-conversion-naming)); failure outranks unverified.

**Review copies are not deliverables.** Do not rename them into place. How a review copy could be accepted will be decided after a separately authorized review of real discs; until then, rerun the ISO once the cause is understood. Reruns never overwrite review copies, and review copies do not trigger the overwrite prompt.

**A passed check is not proof of a complete copy.** It means only that the MP4's length matches the expected duration within the tolerance — not that picture and sound are intact, nor that the disc was captured the way a DVD player would play it (see [Menu VOBs Are Intentionally Excluded](#menu-vobs-are-intentionally-excluded)).

**Promotion is not one transaction.** The final renames happen one titleset after another. If one fails or is interrupted after earlier ones succeeded, promotion stops, the run fails, and its records list exactly which final MP4s this run replaced or created. An existing log and manifest then describe an earlier run and no longer match those files, and the run's warning says so. Records are written only after promotion, so a run can also fail after a partial or complete promotion because its records cannot be written. The MP4s it promoted then stay; the canonical log and manifest may be missing, incomplete or from an earlier run; the run may have no records of its own, and the error is shown on screen (a batch counts the ISO as failed). Nothing is rolled back.

| Exit code | Meaning |
|-----------|---------|
| `0` | Every ISO converted (all titlesets passed and promoted), skipped, or a successful dry run |
| `1` | Any failure or unverified result, an operator abort, or another error (see the log) |
| `2` | An option or value the argument parser rejected (an unknown option, or a missing or invalid value). Other arguments given without `-i`/`--iso` or `--batch` currently exit `1` |

### Failed Conversion Naming

Encodes write to a temporary `*.encoding-<random>.mp4` file that atomically replaces the final name only after every titleset of the ISO passes its check — until promotion begins, failed or unverified attempts leave existing final MP4s unchanged. All artifacts of one attempt share a `<timestamp>-<random>` token, prefixed `failed-`, `unverified-` or (for operator aborts) `aborted-`, so retries never overwrite earlier evidence:

```
JPC_AV_00001_failed-20260612T150000-3f9a2c.mp4.partial                  # failed encode
JPC_AV_00001_title02_unverified-20260612T150000-3f9a2c.mp4.unverified   # review copy, not a deliverable
JPC_AV_00001_failed-20260612T150000-3f9a2c.log.txt                      # log for that attempt
JPC_AV_00001_failed-20260612T150000-3f9a2c_manifest.json                # manifest for that attempt
```

A run that ends unverified names its log and manifest `…_unverified-<token>.log.txt` and `…_unverified-<token>_manifest.json`. Kept files never replace an existing file: each name is reserved first, and if it is taken another random suffix is added (`…-3f9a2c-7b01de.mp4.partial`).

A hard kill (power loss, `kill -9`) may leave a `*.encoding-*.mp4` temp file behind, as can a filesystem failure that prevents moving it to its kept name (in that case the run's records name the leftover file and say what it holds). Temp files left by a hard kill are incomplete by definition.

When ffmpeg fails or is interrupted (Ctrl-C), it is killed and given up to 10 seconds to exit. Only once its exit is confirmed have its output's bytes stopped changing, and only then is the output moved to its `.partial` name; a `.partial` is neither complete nor playable. If the exit cannot be confirmed, the run fails (exit `1`), the ISO's remaining titlesets are not attempted and a batch starts no further ISO, the output keeps its temporary name, and the ISO is left attached. The warning, the log and the manifest (`conversion_status.encoder_cleanup`) give the process ID, the possible output path, and the mount points and devices. That process, its output and the mount may still be active: resolve them before retrying the ISO or detaching it.

A Ctrl-C that arrives just as an output is being moved to its `.partial` or review-copy name is handled: the record names where the output actually is. A second Ctrl-C pressed while that location is still being checked can leave the record naming the temporary name, or no kept file; the kept file still carries the run's token, so look for it beside the run's log and manifest.

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

- **Content VOBs** (`VTS_*_1.VOB`, `VTS_*_2.VOB`, etc.) — contain the actual video content, which a DVD player plays in the order the disc's navigation chooses.

The script automatically skips menu VOBs and concatenates only the content VOBs, per titleset, in file order. A disc whose VIDEO_TS contains *only* menu VOBs is skipped (nothing to convert). **The MP4 is not a player-equivalent copy.** File order is not necessarily how a DVD player plays the disc: program order, branching, angles and still frames are not followed, so an MP4 can differ from what a player shows, and a passed duration check does not prove that all intended content was captured. **Nothing is lost from the preservation ISO:** the full disc — including all menu structure, navigation data, logos, and interactive elements — remains intact there, and it can be mounted on any computer to experience the disc exactly as originally authored, menus and all.

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

For skipped ISOs (non-VIDEO_TS content), each skip leaves its own pair of records (a dry run's are named `…_dryrun-…` the same way):

```
/path/to/isos/
├── data_disc.iso
└── access_JPC_AV_data_disc/
    ├── data_disc_skipped-20260612T150000-3f9a2c.log.txt         # Log for that skip
    └── data_disc_skipped-20260612T150000-3f9a2c_manifest.json   # Documents why the disc was skipped
```

For a run that fails or ends unverified before promotion begins, the final MP4s, log and manifest above are left as they were. A run that fails once promotion has begun — a rename stops partway, or its records cannot be written afterwards — can leave MP4s it already promoted beside a missing, incomplete or outdated log and manifest (see [Duration Check and Outcomes](#duration-check-and-outcomes)). The run's own files are named as in [Failed Conversion Naming](#failed-conversion-naming).

---

## verifyiso.py — Collection Fixity Verification

Audits existing ISO preservation masters **without reloading any discs**: it re-hashes each ISO and compares against the source hash recorded in its manifest at imaging time. The `source_hash` was always computed by streaming the physical disc, so it is a valid fixity baseline for ISOs made by *any* version of `makeiso.py`; newer manifests' SHA-256 hashes are compared too when present.

Sudo-free, and it never changes an existing file. `--csv` creates one **new** report file: its path is reserved before anything is hashed, and an existing path (a file, folder, symlink or `/dev/stdout`) or a name ending in `.iso` or `_manifest.json` is refused with exit `1`. The reserved report is never read as a manifest or hashed as a master: a manifest that names the report path as its ISO, directly or through a symlink, is reported `ERROR`, and the report is then not kept (exit `1`); so is a manifest for which that could not be checked (an inspection error other than "no such file" on the manifest or the ISO it names). The report is announced as written only while its path still names the file the run wrote; if that path is replaced or removed during the run, nothing there is removed and the run exits `1`. These checks guard one operator's run against accidental collisions: a change to the path after the last check is not detected. Give each report a new name, for example with the date.

```bash
# Verify everything under a collection folder (searched recursively)
python3 verifyiso.py /Volumes/Archive/JPC_AV

# Multiple locations, with a CSV report for record-keeping
python3 verifyiso.py /Volumes/Archive/JPC_AV /Volumes/Backup/JPC_AV --csv "fixity_$(date +%Y%m%d_%H%M%S).csv"
```

**What is searched.** Every folder under each given path is listed, one at a time, and anything that keeps the search from being complete is reported rather than skipped:
- a path or folder that cannot be checked or listed, including a macOS privacy denial ("Operation not permitted": give Terminal access to removable volumes);
- a listing that stops partway (the entries already read are still used);
- an entry that cannot be inspected;
- a folder that disappears while the search runs.

The rest of the search goes on, and the bundles it finds are verified, but the run then exits `1` even if every one of them passed. No folder is skipped because of its name. So give the collection folder, not a volume or share root: their system and recovery folders (`.Spotlight-V100`, `.Trashes`, `lost+found`, `#recycle` and the like) often cannot be listed by a normal user, and each one would be reported.

Symlinks inside a search are never followed:
- **a link to a folder, or a dangling link:** listed as `EXCLUDED` and fails the run, unless its target folder was itself searched through one of the given paths;
- **a symlink loop:** listed, but holds nothing and does not fail the run;
- **a symlink given as a path:** followed, so giving a link's target, or the link itself, is how to include a linked folder;
- **Finder aliases:** ordinary files, not followed.

A given file must be a `*_manifest.json`; anything else given is reported, not silently ignored. Give folders or manifests, not a shell glob that also matches loose files. A manifest reached through more than one given path is checked once.

Every `*_manifest.json` found is examined, whatever it is called; what it is comes from its content. `makeiso.py` preservation records are checked; `makeiso-video.py` access-copy records and macOS AppleDouble (`._*`) metadata files are skipped. A FIFO, device or socket named like a manifest is never opened (`ERROR`). Masters kept in `*_overwritten-*` archive folders are checked like any other. Each manifest is reported as one of:

| Status | Meaning |
|--------|---------|
| `PASS` | ISO hash matches the manifest (MD5, plus SHA-256 when recorded) |
| `FAIL` | Hash mismatch — the ISO has changed since imaging (bit rot, bad copy, truncation) |
| `MISSING_ISO` | Manifest exists but its ISO is gone |
| `UNVERIFIABLE` | The record cannot be trusted to verify: missing or unrecognized run status, missing integrity record, missing MD5 source hash, a blank, non-text or malformed hash (including a declared SHA-256 with no SHA-256 hash), fields of the wrong type, an ISO name outside its folder, a dry-run record next to an existing ISO (a dry run replaced that master's own manifest), markers of both record families, or a manifest that is neither a `makeiso.py` nor a `makeiso-video.py` record |
| `SKIPPED` | Nothing to check: an explicitly `failed` or `verification_failed` run, a dry-run record with no ISO beside it, a `makeiso-video.py` record, or an AppleDouble metadata file |
| `ERROR` | Manifest unreadable, not valid UTF-8 or JSON, or not a JSON object; the ISO could not be read; or a FIFO, device or socket named like a manifest, or a manifest-named symlink whose target cannot be checked (not opened); or a manifest, or the ISO it names, that is this run's `--csv` report or could not be checked for that (not read or hashed). One bad item never stops the run |

Parts of the search are reported separately, never as manifest results. They are listed before checking starts, again at the end, and as their own rows in the CSV report (path at the start of `detail`; no manifest, ISO or hashes):

| Status | Meaning |
|--------|---------|
| `DISCOVERY_ERROR` | Part of a given path could not be searched (see above), or a given path is not a folder or `*_manifest.json` file |
| `EXCLUDED` | A symlinked folder or dangling symlink that was not followed, and whose target was not searched through a given path |

| Exit code | Meaning |
|-----------|---------|
| `0` | Every checked ISO verified (skips are not failures), and every folder under the given paths was searched; symlinks are not followed, so a symlinked folder counts only when its target was searched through a given path |
| `1` | At least one `FAIL`, `MISSING_ISO`, `UNVERIFIABLE` or `ERROR`, `DISCOVERY_ERROR` or `EXCLUDED` (all listed at the end of the run); no manifests were found; or the `--csv` report was refused or could not be written |
| `130` | Interrupted with Ctrl-C; an unfinished report is removed |

Suitable for a scheduled fixity-audit job, provided each run gets a new report name.

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

#### Run ends UNVERIFIED

Nothing was promoted, and existing final MP4s and records were left as they were. The run's `*_unverified-<token>.log.txt` gives each titleset's reason. Review copies (`*.mp4.unverified`) are not deliverables — see [Duration Check and Outcomes](#duration-check-and-outcomes).

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

Phase 2 — Verification (after asking for the file to be flushed to its media)

┌──────────────┐     ┌──────────────┐     ┌────────────┐
│   ISO File   │────▶│  4MB Chunk   │────▶│  MD5 Hash  │
│  re-read,    │     │              │     │   (iso)    │
│ (F_NOCACHE)  │     └──────────────┘     └────────────┘
└──────────────┘
```

During creation, each 4MB chunk is read from the raw device, written to the ISO file, and fed to the source (raw) hashers — both MD5 and SHA-256. After the copy completes, the script calls `F_FULLFSYNC` on the file, which asks macOS and the drive to write it to the media, drive cache included (a drive or network volume may not honour it). It then re-reads the written ISO with `F_NOCACHE` set, which asks macOS not to cache that read (data the write left cached may still be served), and hashes it independently with both algorithms. If `F_FULLFSYNC` is unavailable the script falls back to plain `fsync`, which may stop at the drive's cache; if `F_NOCACHE` is unavailable the re-read may come from the page cache. Either fallback is shown on screen and recorded as a warning (the run ends `success_with_warnings`), and the log and manifest record what was applied (`flush_before_verification`, `verification_read_cache_bypass`). A match requires both digest pairs to agree; MD5 is retained for continuity with existing collection manifests, SHA-256 for current archival fixity practice.

Because the second hash comes from re-reading the written file — not from the in-memory stream — a mismatch catches write errors and truncated writes, and destination-disk corruption as far as the flush and the re-read reached the media. Neither control is proof that the bytes are on the physical media. Verification always runs; there is no option to skip it.

### Manifest Structure

The JSON manifest (`*_manifest.json`) includes:

- `backup_metadata` — Run ID, UUID, timestamps, operator
- `backup_status` — Success/failure, verification results, and `iso_created`: `true` or `false` when the ISO's presence was checked, `null` when it could not be checked (its size fields are then `null` too, and a warning gives the reason)
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
- `titlesets` — Every planned titleset: its outcome; its duration check (expected and measured durations, their source, the tolerance, and the IFO and VOB-probe evidence); its intended final path; whether this run promoted it (and replaced an earlier file); and where any review copy or `.partial` actually is
- `video_technical_info` — Resolution, frame rate, duration
- `archival_notes` — Purpose, preservation relationship

### Supported Media Types

- CD-ROM / CD-R / CD-RW
- DVD-ROM / DVD±R / DVD±RW
- BD-ROM / BD-R / BD-RE (Blu-ray)

For `makeiso-video.py`, only DVD-Video discs with VIDEO_TS folders are converted to MP4. Other disc types are detected and skipped with documentation.

---

## License

See [LICENSE](LICENSE) for the license terms.

This project is part of the Johnson Publishing Company Archive (JPCA) digitization and preservation initiative, a collaboration between the [Getty Research Institute (GRI)](https://www.getty.edu/projects/johnson-publishing-company-archive/) and the [Smithsonian National Museum of African American History and Culture (NMAAHC)](https://www.searchablemuseum.com/johnson-publishing-company-image-power/).

---

## Contributing

Issues and pull requests welcome at:  
https://github.com/JPC-AV/optical_disc_jpc_av

---

*Built by NMAAHC for audiovisual archival preservation workflows supporting the Johnson Publishing Company Archive.*