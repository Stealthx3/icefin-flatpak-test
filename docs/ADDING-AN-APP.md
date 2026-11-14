# Adding an Icesuite app

1. Add `catalog/<slug>.json` (the slug is the file name, lowercase with dashes) and its icon in `icons/`.
2. Open a pull request on `main`. CI runs the end-to-end test, which loads every catalog file.
3. After merge, publish its first build ([PUBLISHING.md](PUBLISHING.md)). The app appears on the page once it has a
   build. The existing remotes pick it up; users add nothing new.

## Catalog file
```json
{
  "slug": "example-app",
  "name": "Example App",
  "summary": "One line, shown under the name (F-Droid cuts it at 80 characters)",
  "description": "A sentence or two for F-Droid and the page.",
  "license": "GPL-3.0-or-later",
  "icon": "icons/example-app.svg",
  "homepage": "https://github.com/Stealthx3/example-app",
  "issues": "https://github.com/Stealthx3/example-app/issues",
  "changelog": "https://github.com/Stealthx3/example-app/blob/main/CHANGELOG.md",
  "docs": "https://github.com/Stealthx3/example-app/blob/main/docs/INSTALL.md",
  "categories": ["Multimedia"],
  "formats": {
    "flatpak": { "app_id": "io.github.stealthx3.ExampleApp", "needs_flathub": true },
    "deb":     { "package": "example-app" },
    "android": { "package": "io.github.stealthx3.exampleapp" },
    "windows": {}
  },
  "notes": ["Simple HTML is allowed here, for example a <a href=\"…\">link</a>."]
}
```

| Field | Required | Used for |
|---|---|---|
| `slug`, `name`, `summary`, `homepage` | yes | everything |
| `formats.<format>` | per format published | the tool refuses to publish a format the catalog doesn't list |
| `formats.flatpak.app_id` | for Flatpak | which refs to copy from the build repo; the `.flatpakref` |
| `formats.flatpak.needs_flathub` | no (default true) | the page tells users to add Flathub for the runtime |
| `formats.deb.package` | no | a warning if the published `.deb` has another name |
| `formats.android.package` | for Android | F-Droid metadata; must equal the APK's application ID |
| `description`, `license`, `categories`, `issues`, `changelog` | no | F-Droid metadata and page links |
| `notes` | no | bullet points under the app on the page (trusted HTML) |

## Rules worth keeping
- **Flatpak app IDs** use `io.github.stealthx3.<Name>`, so they never clash with Flathub's.
- **Android** release keys are per app and permanent; F-Droid refuses an update signed with another key.
- **Debian versions** for pre-releases use `~` (`1.0.0~alpha1`) so they sort before the final `1.0.0`.
- **Windows** versions become release tags (`<slug>-v<version>`); anything besides letters, digits, `.`, `_` and `-`
  turns into `-`.
