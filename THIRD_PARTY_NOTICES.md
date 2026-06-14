# Third-Party Notices

AoE4 Replay Launcher contains original project code and documentation, and also
uses or interacts with third-party software, services, names, trademarks, and
game-related material.

The MIT License in [`LICENSE`](LICENSE) applies only to the original AoE4 Replay
Launcher code and documentation. It does not relicense any third-party software,
game files, visual assets, names, logos, trademarks, services, or other material.

Nothing in this file grants rights beyond those provided by the applicable
third-party license or terms.

## Age of Empires IV and Microsoft game content

Age of Empires IV © Microsoft Corporation. AoE4 Replay Launcher was created
under Microsoft's "Game Content Usage Rules" using assets from Age of Empires IV,
and it is not endorsed by or affiliated with Microsoft.

Microsoft's Game Content Usage Rules:

https://www.xbox.com/en-US/developers/rules

Age of Empires IV names, trademarks, logos, civilization imagery, rank imagery,
screenshots, and other game-related material remain the property of Microsoft
and their respective rights holders. Such material is not covered by this
project's MIT License.

This repository and its release packages do not contain or redistribute
Age of Empires IV executables, depot content, historical builds, asset archives,
or other proprietary game files. When historical files are required, they are
downloaded directly from Steam on the user's computer using a Steam account that
owns the game.

Microsoft's Game Content Usage Rules govern any game-related visual material
included with or displayed by this project. Those rules do not grant permission
to redistribute proprietary game binaries or depot content.

## Steam and Valve

Steam and Valve are trademarks or registered trademarks of Valve Corporation.
AoE4 Replay Launcher is not endorsed by, sponsored by, or affiliated with Valve
Corporation or Steam.

Access to Steam services and content remains subject to the user's agreements
with Valve and to any terms applicable to the relevant game and depot content.

## AoE4World API

AoE4 Replay Launcher uses the public AoE4World API to search for players and
retrieve public match information displayed by the launcher.

AoE4World is an independent, fan-made community service. It is not part of
AoE4 Replay Launcher, and its API, website, data, names, logos, and services are
not covered by this project's MIT License.

Official website:

`https://aoe4world.com`

API documentation and usage guidance:

`https://aoe4world.com/api`

AoE4 Replay Launcher is not endorsed by, sponsored by, or affiliated with
AoE4World or its maintainers.

Use of the AoE4World API remains subject to AoE4World's current policies and
usage guidance. The launcher should make responsible requests, avoid unnecessary
bulk collection, cache responses where appropriate, and identify itself through
an application-specific User-Agent.


## restic

restic is a separate third-party program used for deduplicated storage. It is
not part of the original AoE4 Replay Launcher code and is not covered by this
project's MIT License.

The launcher downloads restic from its official release source when required;
restic is not embedded in this repository's application source.

Official project:

https://github.com/restic/restic

License: BSD 2-Clause License

Authoritative license text:

https://github.com/restic/restic/blob/master/LICENSE

Copyright (c) 2014, Alexander Neumann <alexander@bumpern.de>

## DepotDownloader

DepotDownloader is a separate third-party program used to access Steam depot
content. It is not part of the original AoE4 Replay Launcher code and is not
covered by this project's MIT License.

The launcher downloads an official DepotDownloader release when required;
DepotDownloader is not embedded in this repository's application source.

Official project:

https://github.com/SteamRE/DepotDownloader

License: GNU General Public License version 2.0

Authoritative license text:

https://github.com/SteamRE/DepotDownloader/blob/master/LICENSE

## RunAsDate

RunAsDate is proprietary freeware by Nir Sofer. It is not open-source software
and is not covered by this project's MIT License.

AoE4 Replay Launcher does not modify RunAsDate. When required, the application
downloads the complete, unmodified x64 distribution package from NirSoft's
official page:

https://www.nirsoft.net/utils/run_as_date.html

NirSoft's published terms permit free redistribution only when no fee is
charged and the complete distribution package is included without modification.
They do not permit selling RunAsDate as part of a software package. NirSoft's
current published terms control and may change over time.

## Bundled Python runtime and libraries

Release builds may include a Python runtime, Tcl/Tk, and Python libraries inside
the packaged application. These components remain under their own licenses and
are not relicensed under the AoE4 Replay Launcher MIT License.

Known direct runtime components include:

| Component | License | Official license or project |
|---|---|---|
| Python | Python Software Foundation License Version 2 | https://docs.python.org/3/license.html |
| Tcl/Tk | Tcl/Tk License | https://www.tcl-lang.org/software/tcltk/license.html |
| Typer | MIT License | https://github.com/fastapi/typer/blob/master/LICENSE |
| CustomTkinter | MIT License | https://github.com/TomSchimansky/CustomTkinter/blob/master/LICENSE |
| Pillow | MIT-CMU License | https://github.com/python-pillow/Pillow/blob/main/LICENSE |

PyInstaller may be used as a build tool to create release packages. PyInstaller
uses GPL-2.0 with a special exception that permits generated executable bundles
to be distributed under the application's chosen license, subject to the
licenses of the bundled dependencies:

https://pyinstaller.org/en/stable/license.html

Python packages may bring additional transitive dependencies. Every release
builder or redistributor is responsible for preserving all notices and license
texts required by the exact dependency versions included in that release.

## Other third-party material

Any additional third-party code, fonts, icons, images, data files, APIs, or
services retain their respective copyrights, licenses, and terms. Their
inclusion, use, or reference does not imply endorsement of AoE4 Replay Launcher.

## No endorsement

AoE4 Replay Launcher is an independent, unofficial community project. It is not
endorsed by, sponsored by, or affiliated with Microsoft, World's Edge, Relic
Entertainment, Valve Corporation, Steam, restic, SteamRE, NirSoft, or the
maintainers of the listed Python libraries.
