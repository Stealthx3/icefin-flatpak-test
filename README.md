# Icesuite test repository

**Install page: <https://stealthx3.github.io/icesuite-test/>**

One repository that serves alpha and test builds of every Icesuite app, in four formats:

| Format | How users get it | Updates | Where it lives |
|---|---|---|---|
| Flatpak | `flatpak remote-add icesuite-test …` once, then `flatpak install` | `flatpak update` | `flatpak/repo/` on Pages (signed OSTree repo) |
| Debian / Ubuntu `.deb` | an APT source, once | `apt upgrade` | `apt/` on Pages (signed APT repo) |
| Android `.apk` | an F-Droid repository, once (F-Droid, Droid-ify, Neo Store) | the store app | `fdroid/repo/` on Pages (signed F-Droid index) |
| Windows `.exe` / `.msi` | download from the install page | reinstall | GitHub pre-releases on this repository, plus `windows/<app>.json` on Pages |

Users only ever add a repository once per platform. A new Icesuite app shows up in the same remotes.

## Goals
- One place to test every Icesuite project (Icefin Player, IceRom, and whatever comes next), with no new repository per app.
- Real package-manager updates on every platform that has one, so testers stay on the newest build without doing anything.
- Every repository signed: one OpenPGP key for Flatpak and APT, one keystore for F-Droid.
- Stay inside GitHub's limits: a 1 GB Pages site, 100 MB per file, and a git history that doesn't grow with every build.

## How it is laid out
- **`main` (this branch)** is the source: the publish tool, the app catalog, the config, icons and the public key.
- **`gh-pages`** is the generated site that GitHub Pages serves. Every publish replaces it with **one parentless
  commit**, so the 200+ MB of build objects are never stacked in history. Don't edit it by hand.

```
config.json            site URL, repository names, signing key fingerprint, retention
catalog/<app>.json     one file per Icesuite app: name, links, package IDs per format
icons/                 app icons used on the page and in .flatpakref files
keys/icesuite-test.gpg the public OpenPGP key (Flatpak + APT)
tools/icesuite.py      the publisher: sync, publish-flatpak/deb/apk/windows, push
tools/test-e2e.sh      end-to-end test with throwaway keys (also run by CI)
docs/                  publishing guide, adding an app, migration, decision log
```

## Publishing (short version)
In the lab guest that holds the signing keys:
```bash
tools/icesuite.py sync
tools/icesuite.py publish-flatpak --app icefin-player --src-repo ~/fp-repo --version 0.1.0-alpha.3 --commit abc1234
tools/icesuite.py publish-deb     --app icefin-player --commit abc1234 icefin-player_0.1.0~alpha3_amd64.deb
tools/icesuite.py publish-apk     --app some-app --commit abc1234 app-release.apk
tools/icesuite.py publish-windows --app some-app --version 0.1.0-alpha.3 --commit abc1234 SomeApp-Setup.exe
tools/icesuite.py push
```
Full guide: [docs/PUBLISHING.md](docs/PUBLISHING.md). New app: [docs/ADDING-AN-APP.md](docs/ADDING-AN-APP.md).

## Apps
| App | Source | Formats in the catalog |
|---|---|---|
| Icefin Player | [Stealthx3/icefin-player](https://github.com/Stealthx3/icefin-player) | Flatpak, .deb |

## History
This repository started as `icefin-flatpak-test`, a Flatpak-only remote for Icefin Player. The move to a multi-app,
multi-format repository is described in [docs/MIGRATION.md](docs/MIGRATION.md); decisions are logged in
[docs/DECISIONS.md](docs/DECISIONS.md).

Signing key: "Icefin Player test repository", ed25519, fingerprint `532C A3B2 8FBA 72FC 9C9F 369C 1CD5 3BAD 4D48 EB54`
(kept from the Flatpak-only days so existing installs keep trusting it).
