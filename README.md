# AoE4 Replay Launcher

Watch **old Age of Empires IV replays** that the current game version can no
longer open.

Age of Empires IV replays (`.rec`) only work with the exact game build on which
they were recorded. AoE4 Replay Launcher identifies that build, reconstructs it
from official Steam depot files, and launches the game using the replay's
original date so the historical version can run correctly.

Replays recorded on your currently installed patch open immediately—no download
or Steam sign-in required.

> **Windows only.** Requires a legitimately owned Steam copy of Age of Empires IV.

## Highlights

- Find, download, import, and play replays from a desktop panel.
- Browse a player's games or list head-to-head matches between two players.
- Automatically reconstruct the exact historical build required by a replay.
- Store historical build data efficiently in a deduplicated
  [restic](https://restic.net/) repository.
- Cache builds for reuse or save selected builds for instant offline playback.
- Automatically update the replay-to-build map as new patches are released.

## How it works

```text
   replay.rec
       │  read the recorded game build
       ▼
   build map ──► resolve the Steam depot manifest
       │
       ▼
   Steam depots ──► download the required historical files
       │
       ▼
   restic repository ──► restore the build's delta
       │
       ▼
   compose launch build = current Steam install (hardlinked seed)
                        + restored historical delta (hardlinked)
       │
       ▼
   launch through RunAsDate using the replay's date
```

The restic repository stores only the files that differ from the current live
installation—the build's **delta**. At launch, unchanged files are hardlinked
from your normal AoE4 installation and combined with the restored historical
files. This avoids keeping a complete copy of the game for every patch.

The first launch of a historical build may require a Steam download, followed by
restoration and composition on disk. Download time depends mainly on your
connection; composition time depends mainly on SSD or disk performance. Once the
delta is stored, it does not need to be downloaded again.

Composed builds are cached while the launcher is open. After playback, you can
also **save** a build to keep it across sessions for instant, offline reuse.
Unsaved composed builds are cleaned up when the launcher closes, while their
deduplicated data remains in restic and can be reconstructed later.
Does not require re-downloading.

A scheduled job records newly released Steam manifests, and the launcher
refreshes its build map at startup. Newly released patches can therefore be
supported without requiring a new application release.

## Steam sign-in and privacy

Steam authentication is requested only when files from a historical build must
be downloaded. Steam requires the account to own Age of Empires IV before those
depot files can be accessed.

The recommended method is the Steam Mobile QR flow: scan the code and approve
the sign-in in the Steam app, without entering a password into the launcher. A
username and password flow is also available when necessary.

Authentication is handled through Steam's downloader flow. The launcher does
not store your Steam password. Steam's remembered-login data and your account
name may be cached locally so future downloads can run without another sign-in.

## Desktop panel

Open **`AoE4-Replay-Launcher.exe`** from a release build, or
**`AoE4-Replay-Launcher.vbs`** from a source installation.

The panel can:

- look up a player by name or profile ID and browse their games page by page;
- filter games by date range;
- list head-to-head matches between two players;
- show each match's date, mode, map, civilizations, and winner;
- download replays through the official Relic API;
- import existing `.rec` and `.gz` files;
- play or delete downloaded replays;
- manage saved historical builds and display their disk usage.

## Getting started

### Release build—no Python or setup required

1. Download the latest **AoE4 Replay Launcher** release from the
   [Releases](https://github.com/EKYavsil/aoe4-replay-launcher/releases) page.
2. Extract the **entire folder** to a writable location such as your Desktop.
   Do not run it from inside the archive or place it under `Program Files`.
3. Double-click **`AoE4-Replay-Launcher.exe`**.

Keep the extracted folder together. The application stores its configuration,
downloaded tools, replay data, and restic repository beside the executable.
Python is not required; external tools are downloaded from their official
sources when first needed.

The release is not code-signed. On first run, Windows SmartScreen may display
**“Windows protected your PC.”** Select **More info**, then **Run anyway**. This
is expected for an independently distributed open-source application without a
commercial code-signing certificate.

### From source—developers and advanced users

```console
git clone https://github.com/EKYavsil/aoe4-replay-launcher
cd aoe4-replay-launcher
```

Run **`setup.bat`**. It locates Python 3.12 or newer—or offers to install it
through `winget`—creates a virtual environment, installs the project in editable
mode, and creates `config.local.toml`.

Because the project is installed in editable mode, changes from `git pull` take
effect without running setup again.

Open the GUI with **`AoE4-Replay-Launcher.vbs`**, or use the CLI:

```console
aoe4replay watch "C:\path\to\replay.rec"
```

## Commands

| Command | Purpose |
|---|---|
| `aoe4replay panel` | Open the desktop panel. |
| `aoe4replay watch <replay.rec>` | Reconstruct the required build and play the replay. |
| `aoe4replay add <replay.rec>` | Download and store the required build without launching it. |
| `aoe4replay ingest <cache-dir>` | Import an existing `delta_*` cache into restic. |
| `aoe4replay check` | Verify the integrity of the restic repository. |
| `aoe4replay list` | List stored builds and restic snapshots. |
| `aoe4replay reindex` | Rebuild the reusable-file index. |

## Storage and deduplication

Age of Empires IV asset archives (`.sga`) make up most of the historical build
cache. They are already compressed, so ordinary compression provides little
benefit. The main reduction comes from **content-defined deduplication**:
consecutive versions of the same archives share much of their underlying data.

Measurements from the delta cache for approximately one year of historical
builds:

| Measurement | Size |
|---|---:|
| Raw delta data | ~140 GB |
| Stored after deduplication | ~55 GB |
| **Disk space saved** | **~61%** |

The first-time Steam download for an individual build may be larger than the
amount ultimately added to restic because restic reuses chunks already stored
from other builds.

## External tools

External executables are downloaded from their official sources when first
needed rather than embedded in the launcher.

| Tool | Official source | Distribution |
|---|---|---|
| restic | [GitHub releases](https://github.com/restic/restic/releases) | Automatically downloaded; BSD-2-Clause |
| DepotDownloader | [GitHub releases](https://github.com/SteamRE/DepotDownloader/releases) | Automatically downloaded |
| RunAsDate | [NirSoft](https://www.nirsoft.net/utils/run_as_date.html) | Complete x64 package automatically downloaded; not bundled |

## License and third-party notice

The original source code and documentation in this repository are licensed under
the MIT License.

Third-party tools, dependencies, game files, assets, names, and trademarks remain
subject to their respective licenses and terms. See [LICENSE](LICENSE) for
the complete notices.

This project does not distribute Age of Empires IV game files. Historical files
are downloaded directly from Steam for users who own the game and remain subject
to the applicable Steam and game terms.

This project is not endorsed by, sponsored by, or affiliated with Microsoft,
World's Edge, Relic Entertainment, Steam, or Valve.