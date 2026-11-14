# Publishing builds

All publishing runs `tools/icesuite.py` from a checkout of `main`, on a machine that holds the signing keys (the lab
guest `lab-icefin-main` today). It works on a second checkout, the **site** (the `gh-pages` branch, by default
`~/icesuite-site`), and ends with one force-push.

```
sync  →  publish-… (any number, any apps, any formats)  →  push
```

## Publisher setup (once per machine)
Debian 13:
```bash
sudo apt install flatpak ostree apt-utils dpkg-dev gnupg git gh python3 fdroidserver default-jdk-headless
git clone https://github.com/Stealthx3/icesuite-test.git ~/icesuite-test     # main: the tool, catalog, config
```
`fdroidserver` and the JDK are only needed for Android; `gh` (signed in, `gh auth login`) only for Windows.

### Keys
| Key | Signs | Where it lives | Losing it means |
|---|---|---|---|
| OpenPGP `532C A3B2 … 4D48 EB54` (ed25519, no passphrase, expires 2028-11-11) | Flatpak commits and summary, APT `InRelease`/`Release.gpg` | lab guest `~/icefin-test-gpg` (`GNUPGHOME`) | every user re-adds the Flatpak remote and the APT key |
| F-Droid keystore (RSA 4096, PKCS12) | the F-Droid index | lab guest `~/icesuite-fdroid-key/` | every user re-adds the F-Droid repo |

Neither private key ever goes in a repository. Back both folders up together.

Environment for a publishing session:
```bash
export GNUPGHOME=$HOME/icefin-test-gpg          # the OpenPGP key
. ~/icesuite-fdroid-key/env                     # ICESUITE_FDROID_KEYSTORE(_PASS), Android only
export ICESUITE_SITE=$HOME/icesuite-site        # the default; set it to use another folder
cd ~/icesuite-test && git pull
```
Create the F-Droid keystore once, before the first Android publish:
```bash
tools/icesuite.py fdroid-keygen --dir ~/icesuite-fdroid-key
```

### Pushing
`push` uses the site checkout's `origin` (`https://github.com/Stealthx3/icesuite-test.git`, or `$ICESUITE_REMOTE`,
for example `git@github.com:Stealthx3/icesuite-test.git`). If the guest has no GitHub credentials, run `push` (and
`sync`) from the host on a copy of the site folder, or give the guest a fine-grained token limited to this repository.

## Each format

### Flatpak
Build with `flatpak-builder --repo=$HOME/fp-repo …` as usual, then:
```bash
tools/icesuite.py publish-flatpak --app icefin-player --src-repo ~/fp-repo \
  --version 0.1.0-alpha.3 --commit abc1234
```
It copies the app ref and its extensions (`.Locale`; `.Debug` only with `--with-debug`) for every arch and branch in
the source repo (narrow with `--arch`, `--branch`), signs each commit, and updates the summary and the shared appstream
for all apps. `flatpak.prune_depth` (3) keeps the last three commits of each ref so `flatpak update --commit=` can roll
back.

### Debian / Ubuntu
```bash
tools/icesuite.py publish-deb --app icefin-player --commit abc1234 dist/*.deb
```
Packages go to `apt/pool/main/<app>/`. The tool regenerates `Packages` for each architecture in
`config.json` (`amd64`, `arm64`; `Architecture: all` packages appear in each) and signs `Release`. It keeps the
newest `apt.keep_versions` (2) of each package and architecture. Version the packages so they sort in order
(`0.1.0~alpha3`, not `0.1.0-alpha3`, sorts before `0.1.0`).

### Android
APKs must be signed with the app's own release key, the same key every time, or Android refuses the update.
```bash
tools/icesuite.py publish-apk --app some-app --commit abc1234 app/build/outputs/apk/release/app-release.apk
```
The tool writes F-Droid metadata from the catalog, runs `fdroid update`, keeps the newest `fdroid.keep_versions`
(3), and records the repository fingerprint on the page. `formats.android.package` in the catalog must match the APK's
application ID. The version code must go up with every build.

### Windows
```bash
tools/icesuite.py publish-windows --app some-app --version 0.1.0-alpha.3 --commit abc1234 SomeApp-Setup.exe
```
Installers can be over Pages' 100 MB file limit, so they go to a GitHub pre-release on this repository, tagged
`<app>-v<version>` (the tool keeps the newest `windows.keep_releases`, 3). The site gets `windows/<app>.json`
(version, URLs, SHA-256), which an app's own updater can poll. Test installers are not code-signed.

## Check and push
```bash
tools/icesuite.py check      # size budget and everything published
tools/icesuite.py push
```
`push` refuses if the site is over `site_limit_mb` (950 MB) or any file is 100 MB or more, then commits the whole site
as one parentless commit and force-pushes it with a lease. If someone published since your `sync`, the push is
refused instead of overwriting their build: `sync --force`, publish again, push. Pages redeploys in about a minute.

Add `--push` to a `publish-…` command to do it in one step.

## Size budget
GitHub Pages allows 1 GB per site. Rough sizes today: Icefin Player's Flatpak (app + Locale, depth 3) about 220 MB.
If `check` warns, lower `flatpak.prune_depth` to 1 or 2 first; deltas (`flatpak.static_deltas`) cost space and
are off.

## Troubleshooting
- `build-update-repo` stops at `opendir(refs/remotes)`: the tool creates the empty directories git drops; if you ran
  flatpak by hand, `mkdir -p flatpak/repo/refs/remotes flatpak/repo/refs/mirrors` in the site.
- `signing key … does not match the published public key`: `GNUPGHOME` points at the wrong keyring.
- Flatpak clients see the new build a few minutes late: Pages caches files for up to 10 minutes.
- A test run of the whole flow: `tools/test-e2e.sh` (throwaway keys, nothing leaves the machine).
