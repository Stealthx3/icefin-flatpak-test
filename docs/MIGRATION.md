# From icefin-flatpak-test to icesuite-test

`Stealthx3/icefin-flatpak-test` was a Flatpak-only remote for Icefin Player, served by Pages from `main`, with every
build committed on top of the last (211 MB of history after two days). It becomes `Stealthx3/icesuite-test`.

## What changed
| | Before | After |
|---|---|---|
| Pages source | `main`, `/` | `gh-pages`, `/` (one parentless commit per publish) |
| Site URL | `https://stealthx3.github.io/icefin-flatpak-test/` | `https://stealthx3.github.io/icesuite-test/` |
| OSTree repo | `/repo/` | `/flatpak/repo/` (same objects, same signatures) |
| Repo file | `/icefin-test.flatpakrepo` | `/flatpak/icesuite-test.flatpakrepo` |
| Ref file | `/io.github.stealthx3.IcefinPlayer.flatpakref` | `/flatpak/io.github.stealthx3.IcefinPlayer.flatpakref` |
| Remote name suggested | `icefin-test` | `icesuite-test` |
| Public key | `/icefin-test.gpg` | `/keys/icesuite-test.gpg` (the same key) |
| Publishing | `flatpak build-commit-from` by hand, `git commit`, `git push` | `tools/icesuite.py` |

## Cut-over, in order
GitHub doesn't redirect Pages after a rename, so the old URL stops working at step 1. Do steps 1 to 3 together.

1. **Rename** the repository: Settings → General → Repository name → `icesuite-test`. Git remotes keep working
   through GitHub's redirect; update them anyway with `git remote set-url origin https://github.com/Stealthx3/icesuite-test.git`.
2. **Pages source**: Settings → Pages → Build and deployment → Deploy from a branch → `gh-pages`, `/ (root)` → Save.
3. **Merge** the conversion pull request into `main`. (Before step 2, merging would take the site down: `main` no
   longer holds it.)
4. **Each machine with the old remote** (the host and the HTPC):
   ```bash
   flatpak remote-modify --user --url=https://stealthx3.github.io/icesuite-test/flatpak/repo/ icefin-test
   flatpak update --user
   ```
   The installed app, its settings and the trusted key stay. To use the new remote name instead:
   `flatpak remote-delete --force --user icefin-test`, add `icesuite-test` from the install page, then
   `flatpak install --user --reinstall icesuite-test io.github.stealthx3.IcefinPlayer`.
5. **Lab guest**: replace the old `~/pub-site` clone with the new flow ([PUBLISHING.md](PUBLISHING.md)). The
   OpenPGP key and `GNUPGHOME` don't change.
6. **icefin-player docs**: `docs/INSTALL.md` and `docs/dev/TEST-REPO.md` point at the old URL and the old manual
   steps; update them to the new page and to `tools/icesuite.py`.

## Until the next Flatpak publish
The repository summary was signed before the move, so `flatpak remote-ls` still shows the title "Icefin Player
(test)". The first `publish-flatpak` re-signs the summary with the new title. Nothing else waits on it.

## Optional: shrink `main`
`main` still carries the old OSTree objects in its history (about 210 MB); `gh-pages` doesn't need them once it has
its own copy. GitHub doesn't count that toward Pages, only toward the repository's size. Rewriting `main`'s history
(for example `git filter-repo --path repo --invert-paths`, then a force-push) would drop it, at the cost of changing
every commit ID on `main`. Not required.
