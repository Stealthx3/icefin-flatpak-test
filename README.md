# Icefin Player test repository

A Flatpak remote with alpha and test builds of [Icefin Player](https://github.com/Stealthx3/icefin-player), served by GitHub Pages: <https://stealthx3.github.io/icefin-flatpak-test/>

```bash
flatpak remote-add --user --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
flatpak remote-add --user --if-not-exists icefin-test https://stealthx3.github.io/icefin-flatpak-test/icefin-test.flatpakrepo
flatpak install --user icefin-test io.github.stealthx3.IcefinPlayer
```

Updates: `flatpak update`. Full instructions are on the page above.

This repository holds a generated OSTree repository (`repo/`), not source code. The source and issues are in [Stealthx3/icefin-player](https://github.com/Stealthx3/icefin-player). Commits are signed with the key in `icefin-test.gpg` (fingerprint `532C A3B2 8FBA 72FC 9C9F 369C 1CD5 3BAD 4D48 EB54`).
