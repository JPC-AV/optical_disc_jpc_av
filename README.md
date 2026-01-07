# makeiso.py

**Optical Disc to ISO Backup Utility for macOS**

A professional-grade Python utility for creating bit-perfect ISO backups from optical discs (CDs, DVDs, Blu-rays) with comprehensive verification, logging, and archival metadata generation.

---

## Table of Contents

- [Overview](#overview)
- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Quick Start](#quick-start)
- [Usage](#usage)
  - [Interactive Mode](#interactive-mode)
  - [Command Line Options](#command-line-options)
  - [Examples](#examples)
- [Workflow](#workflow)
- [Output Files](#output-files)
- [Understanding the Output](#understanding-the-output)
- [Troubleshooting](#troubleshooting)
- [Technical Details](#technical-details)
- [License](#license)

---

## Overview

`makeiso.py` creates archival-quality ISO images from optical discs while simultaneously verifying data integrity. It's designed for digital preservation workflows where verification and documentation are critical.

The script reads directly from the raw device (bypassing filesystem caching) and calculates MD5 checksums during the copy process.

---

## Features

- **Bit-perfect copying** — Reads from raw device (`/dev/rdiskN`) for true sector-by-sector backup
- **Parallel verification** — MD5 checksums calculated simultaneously during ISO creation
- **Real-time progress** — Live display of speed, elapsed time, and ETA
- **Comprehensive logging** — Detailed operation log with timestamps
- **JSON manifest** — Machine-readable metadata for archival systems
- **Directory tree capture** — Complete file listing from source disc
- **ISO structural analysis** — Validates ISO 9660/UDF structure via isolyzer
- **Automatic disc handling** — Unmounts before backup, remounts and ejects after

---

## Requirements

### System
- **macOS** (uses `diskutil` for disc management)
- **Python 3.7+** (uses dataclasses, f-strings, type hints)
- **sudo privileges** (required for raw device access)

### Dependencies

**Required (included with macOS):**
- `diskutil` — Disc management
- `dd` — Raw device copying

**Required (install separately):**
```bash
# Install tree (for directory listings)
brew install tree
```

**Optional (for ISO structural analysis):**
```bash
# Install isolyzer
pip install isolyzer
```

> **Note:** The script will work without `isolyzer`, but ISO structural validation will be skipped.

---

## Installation

### Step 1: Install Homebrew (if not already installed)

Homebrew is a package manager for macOS that makes installing software easy.

Open **Terminal** (Applications → Utilities → Terminal) and paste:

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

Follow the prompts. When complete, close and reopen Terminal.

### Step 2: Install Python 3

macOS comes with Python, but it's best to install a current version:

```bash
brew install python
```

Verify the installation:

```bash
python3 --version
```

You should see something like `Python 3.11.x` or higher.

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

Now clone the project repository:

```bash
# Navigate to where you want the project files
cd ~/Documents

# Clone the repository
git clone https://github.com/JPC-AV/optical_disc_jpc_av.git

# Enter the project folder
cd optical_disc_jpc_av
```

### Step 5: Install Required Tools

With your virtual environment activated:

```bash
# Install tree (for directory listings)
brew install tree

# Install isolyzer (for ISO structural analysis)
pip install isolyzer
```

### Step 6: Verify Everything is Ready

```bash
# Check Python
python3 --version

# Check tree
tree --version

# Check isolyzer
isolyzer --version
```

If all commands return version information, you're ready to go!

### Running the Script

**Important:** Always activate your virtual environment before running the script:

```bash
# Activate virtual environment (from anywhere)
source ~/venv/bin/activate

# Navigate to the project folder
cd ~/Documents/optical_disc_jpc_av

# Run the script (requires sudo for disc access)
sudo python3 makeiso.py
```

> **Why `sudo`?** The script needs administrator privileges to read directly from the optical drive hardware. You'll be prompted for your Mac password.

---

## Quick Start

1. Insert an optical disc
2. Run the script with sudo:
   ```bash
   sudo python3 makeiso.py
   ```
3. Follow the prompts to select the disc and specify output location
4. Wait for the backup to complete
5. Find your ISO and documentation files in the output directory

---

## Usage

### Interactive Mode

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

### Command Line Options

```
sudo python3 makeiso.py [options]

Options:
  -h, --help              Show help message and exit
  --dry-run               Run without creating ISO (test mode)
  --no-verification       Skip MD5 checksum verification
  --filename NAME         ISO filename (without .iso extension)
  --dir PATH              Output directory (supports ~)
  --operator NAME         Operator name or initials for logging
```

### Examples

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

---

## Workflow

The script executes the following steps:

```
┌─────────────────────────────────────────────────────────────┐
│  1. INITIALIZATION                                          │
│     • Display available disks                               │
│     • Collect user inputs (disk ID, filename, etc.)         │
│     • Validate disk exists and is optical media             │
└─────────────────────────────────────────────────────────────┘
                              ▼
┌─────────────────────────────────────────────────────────────┐
│  2. TREE LISTING                                            │
│     • Generate directory structure of mounted volume        │
│     • Save to {filename}_tree.txt                           │
└─────────────────────────────────────────────────────────────┘
                              ▼
┌─────────────────────────────────────────────────────────────┐
│  3. UNMOUNT                                                 │
│     • Safely unmount disc (required for raw access)         │
└─────────────────────────────────────────────────────────────┘
                              ▼
┌─────────────────────────────────────────────────────────────┐
│  4. ISO CREATION + VERIFICATION (parallel)                  │
│     • Read from raw device (/dev/rdiskN)                    │
│     • Write to ISO file                                     │
│     • Calculate MD5 of written data                         │
│     • Calculate MD5 of source stream                        │
│     • Display real-time progress                            │
└─────────────────────────────────────────────────────────────┘
                              ▼
┌─────────────────────────────────────────────────────────────┐
│  5. STRUCTURAL ANALYSIS                                     │
│     • Run isolyzer on created ISO                           │
│     • Validate ISO 9660 / UDF structure                     │
│     • Save raw XML to {filename}_isolyzer.xml               │
└─────────────────────────────────────────────────────────────┘
                              ▼
┌─────────────────────────────────────────────────────────────┐
│  6. FINALIZATION                                            │
│     • Remount disc                                          │
│     • Eject disc                                            │
│     • Generate formatted log file                           │
│     • Generate JSON manifest                                │
│     • Display summary                                       │
└─────────────────────────────────────────────────────────────┘
```

---

## Output Files

For a backup with filename `MyDisc`, the script creates a directory containing:

```
~/Backups/MyDisc/
├── MyDisc.iso              # The ISO image
├── MyDisc.iso.log.txt      # Human-readable operation log
├── MyDisc_manifest.json    # Machine-readable metadata
├── MyDisc_tree.txt         # Directory listing of source disc
└── MyDisc_isolyzer.xml     # Raw ISO structural analysis
```

### File Descriptions

| File | Purpose |
|------|---------|
| `*.iso` | Bit-perfect disc image, mountable on any system |
| `*.iso.log.txt` | Detailed log with timestamps, commands run, checksums, and verification results |
| `*_manifest.json` | Comprehensive JSON metadata for archival systems and databases |
| `*_tree.txt` | Complete directory listing with file sizes, permissions, and dates |
| `*_isolyzer.xml` | ISO 9660/UDF structural validation from isolyzer tool |

---

## Understanding the Output

### Terminal Output

During execution, you'll see color-coded status updates:

- 🔵 **Blue** — Section headers
- 🔷 **Cyan** — Information and commands being run
- 🟡 **Yellow** — File paths and values
- 🟢 **Green** — Success messages
- 🔴 **Red** — Errors

### Progress Display

```
ISO Creation: 2048MB / 4096MB
Elapsed: 0:01:23
Remaining: 0:01:24
Avg Speed: 24.67MB/s
```

### Verification Output

```
MD5 (ISO):      a1b2c3d4e5f6...
MD5 (Raw Disk): a1b2c3d4e5f6...
Checksum match: ISO is a true bit-for-bit copy.
```

### Exit Codes

| Code | Meaning |
|------|---------|
| `0` | Success — backup completed and verified |
| `1` | Failure — backup failed (see error message) |
| `2` | Warning — backup completed but checksum mismatch detected |

---

## Troubleshooting

### "This script must be run with sudo"

Raw device access requires root privileges:
```bash
sudo python3 makeiso.py
```

### "Could not get disk info for diskN"

- Verify the disc is inserted and recognized
- Check disk ID with `diskutil list`
- Ensure you're using the correct disk identifier (e.g., `disk2`, not `disk2s1`)

### "isolyzer not found"

Install isolyzer for ISO structural analysis:
```bash
pip install isolyzer
```
The script will still work without it, but structural validation will be skipped.

### "tree: command not found"

Install tree for directory listings:
```bash
brew install tree
```

### Slow backup speed

- External USB 2.0 drives are limited to ~30 MB/s
- USB 3.0 or Thunderbolt drives will be faster
- Damaged discs may cause slowdowns due to read retries

### Checksum mismatch

This indicates the ISO is **not** identical to the source disc. Possible causes:
- Disc read errors (scratches, degradation)
- Drive malfunction
- System memory issues

**Recommendation:** Re-run the backup. If mismatches persist, inspect the disc for physical damage.

---

## Technical Details

### How Verification Works

The script uses **parallel hash calculation** during the copy process:

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│  Raw Device  │────▶│  4MB Chunk   │────▶│   ISO File   │
│ /dev/rdisk2  │     │              │     │              │
└──────────────┘     └──────┬───────┘     └──────────────┘
                           │
                     ┌─────┴─────┐
                     ▼           ▼
              ┌──────────┐ ┌──────────┐
              │ MD5 Hash │ │ MD5 Hash │
              │  (raw)   │ │  (iso)   │
              └──────────┘ └──────────┘
```

Each 4MB chunk is:
1. Read from the raw device
2. Written to the ISO file
3. Fed to **two** MD5 hashers simultaneously

This ensures both hashes are calculated from the same data stream in a single pass.

### Manifest Structure

The JSON manifest includes:

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

### Supported Media Types

- CD-ROM / CD-R / CD-RW
- DVD-ROM / DVD±R / DVD±RW
- BD-ROM / BD-R / BD-RE (Blu-ray)

---

## License

This project is part of the Johnson Publishing Company Archive (JPCA) digitization and preservation initiative, a collaboration between the [Getty Research Institute (GRI)](https://www.getty.edu/projects/johnson-publishing-company-archive/) and the [Smithsonian National Museum of African American History and Culture (NMAAHC)](https://www.searchablemuseum.com/johnson-publishing-company-image-power/).

---

## Contributing

Issues and pull requests welcome at:  
https://github.com/JPC-AV/optical_disc_jpc_av

---

*Built by NMAAHC for audiovisual archival preservation workflows supporting the Johnson Publishing Company Archive.*