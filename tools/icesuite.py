#!/usr/bin/env python3
"""icesuite: publish Icesuite test builds to the shared test repository.

One GitHub repository serves four kinds of package for every Icesuite app:

  Flatpak   a signed OSTree repository        site: flatpak/repo/
  .deb      a signed APT repository           site: apt/
  .apk      a signed F-Droid repository       site: fdroid/repo/
  Windows   installers as GitHub Release assets, plus a JSON feed   site: windows/<app>.json

The site lives on the Pages branch (gh-pages), which every push replaces with a single commit so the git history
never grows. This branch (main) holds this tool, config.json, the app catalog (catalog/*.json), icons and the public
signing key. Typical run, in the lab guest that holds the signing keys:

  tools/icesuite.py sync
  tools/icesuite.py publish-flatpak --app icefin-player --src-repo ~/fp-repo --version 0.1.0-alpha.3 --commit abc1234
  tools/icesuite.py publish-deb     --app icefin-player --commit abc1234 dist/icefin-player_0.1.0_amd64.deb
  tools/icesuite.py push

See docs/PUBLISHING.md.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import functools
import gzip
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GITHUB_FILE_LIMIT = 100 * 1024 * 1024  # GitHub refuses larger files in a push
FORMATS = ("flatpak", "deb", "android", "windows")


# --------------------------------------------------------------------------------------------------------- helpers

def die(msg: str) -> "None":
    print(f"icesuite: error: {msg}", file=sys.stderr)
    sys.exit(1)


def info(msg: str) -> None:
    print(f"icesuite: {msg}", file=sys.stderr)


def run(cmd, **kw) -> subprocess.CompletedProcess:
    cmd = [str(c) for c in cmd]
    info("$ " + " ".join(_redact(cmd)))
    return subprocess.run(cmd, check=True, **kw)


def out(cmd, **kw) -> str:
    cmd = [str(c) for c in cmd]
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw).stdout


def _redact(cmd: list[str]) -> list[str]:
    return [re.sub(r"^(--?\w*pass\w*=).*", r"\1***", c) for c in cmd]


def need(*tools: str) -> None:
    missing = [t for t in tools if not shutil.which(t)]
    if missing:
        die("missing tools: " + ", ".join(missing) + " (see docs/PUBLISHING.md, 'Publisher setup')")


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def safe_tag(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-")


# ---------------------------------------------------------------------------------------------- config and state

class Ctx:
    def __init__(self, args):
        self.config_path = Path(args.config or ROOT / "config.json").resolve()
        self.cfg = json.loads(self.config_path.read_text())
        self.base = self.config_path.parent
        self.cfg["base_url"] = self.cfg["base_url"].rstrip("/") + "/"
        self.catalog_dir = Path(args.catalog or self.base / "catalog").resolve()
        self.catalog = self._load_catalog()
        self.site = Path(args.site or os.environ.get("ICESUITE_SITE") or Path.home() / "icesuite-site").expanduser()
        self.gpg_key = os.environ.get("ICESUITE_GPG_KEY") or self.cfg["gpg"]["fingerprint"]
        self.gnupghome = os.environ.get("GNUPGHOME")

    def _load_catalog(self) -> dict:
        apps = {}
        for f in sorted(self.catalog_dir.glob("*.json")):
            app = json.loads(f.read_text())
            for key in ("slug", "name", "summary", "homepage"):
                if not app.get(key):
                    die(f"{f}: missing '{key}'")
            if app["slug"] != f.stem:
                die(f"{f}: slug '{app['slug']}' must match the file name")
            app.setdefault("formats", {})
            unknown = set(app["formats"]) - set(FORMATS)
            if unknown:
                die(f"{f}: unknown formats {sorted(unknown)}; known: {', '.join(FORMATS)}")
            apps[app["slug"]] = app
        return apps

    def app(self, slug: str, fmt: str | None = None) -> dict:
        if slug not in self.catalog:
            die(f"no app '{slug}' in {self.catalog_dir} (have: {', '.join(self.catalog) or 'none'})")
        app = self.catalog[slug]
        if fmt and fmt not in app["formats"]:
            die(f"catalog/{slug}.json has no formats.{fmt} entry; add one before publishing that format")
        return app

    # paths ----------------------------------------------------------------------------------------------------
    @property
    def public_key(self) -> Path:
        p = os.environ.get("ICESUITE_PUBLIC_KEY") or self.cfg["gpg"]["public_key"]
        return (self.base / p).resolve()

    @property
    def key_name(self) -> str:
        return Path(self.cfg["gpg"]["public_key"]).name

    def url(self, path: str = "") -> str:
        return self.cfg["base_url"] + path

    # state on the site ----------------------------------------------------------------------------------------
    @property
    def builds_path(self) -> Path:
        return self.site / "meta" / "builds.json"

    def builds(self) -> dict:
        return read_json(self.builds_path, {})

    def record(self, slug: str, fmt: str, entry: dict) -> None:
        builds = self.builds()
        builds.setdefault(slug, {})[fmt] = entry
        write_json(self.builds_path, builds)
        hist_path = self.site / "meta" / "history.json"
        hist = read_json(hist_path, [])
        hist.insert(0, {"date": entry["date"], "app": slug, "format": fmt, "version": entry.get("version"),
                        "commit": entry.get("commit")})
        write_json(hist_path, hist[:300])

    def require_site(self) -> None:
        if not (self.site / ".git").exists():
            die(f"no site checkout at {self.site}; run 'icesuite.py sync' first (or set ICESUITE_SITE)")

    # signing --------------------------------------------------------------------------------------------------
    def gpg_args(self) -> list[str]:
        return ["--homedir", self.gnupghome] if self.gnupghome else []

    def check_signing_key(self) -> None:
        need("gpg")
        if not self.public_key.exists():
            die(f"public key {self.public_key} not found")
        pub = out(["gpg", "--batch", "--with-colons", "--show-keys", self.public_key])
        fprs = [l.split(":")[9] for l in pub.splitlines() if l.startswith("fpr:")]
        want = self.gpg_key.replace(" ", "").upper()
        if not any(f.upper().endswith(want) for f in fprs):
            die(f"signing key {self.gpg_key} does not match the published public key {self.public_key}")
        try:
            out(["gpg", "--batch", *self.gpg_args(), "--list-secret-keys", self.gpg_key])
        except subprocess.CalledProcessError:
            die(f"secret key {self.gpg_key} not in the keyring (GNUPGHOME={self.gnupghome or '~/.gnupg'})")


def common_entry(args, version: str | None) -> dict:
    e = {"version": version, "date": now()}
    if getattr(args, "commit", None):
        e["commit"] = args.commit
    if getattr(args, "notes", None):
        e["notes"] = args.notes
    return e


# --------------------------------------------------------------------------------------------------------- flatpak

def flatpak_refs(src: Path, app_id: str, arches, branch, with_debug: bool) -> list[str]:
    refs = []
    for ref in out(["ostree", "refs", f"--repo={src}"]).split():
        ref = ref.split(":", 1)[-1]
        parts = ref.split("/")
        if len(parts) != 4:
            continue
        kind, name, arch, br = parts
        if arches and arch not in arches:
            continue
        if branch and br != branch:
            continue
        if kind == "app" and name == app_id:
            refs.append(ref)
        elif kind == "runtime" and name.startswith(app_id + "."):
            suffix = name[len(app_id) + 1:]
            if suffix == "Sources" or (suffix == "Debug" and not with_debug):
                continue
            refs.append(ref)
    if not any(r.startswith("app/") for r in refs):
        die(f"no app/{app_id}/… ref in {src} matching arch={arches or 'any'} branch={branch or 'any'}")
    return sorted(refs)


def flatpak_update_repo(ctx: Ctx, repo: Path) -> None:
    fc = ctx.cfg["flatpak"]
    cmd = ["flatpak", "build-update-repo", f"--gpg-sign={ctx.gpg_key}",
           f"--title={ctx.cfg['name']}", f"--comment={ctx.cfg['description']}",
           f"--homepage={ctx.url()}", f"--default-branch={fc['default_branch']}",
           "--prune", f"--prune-depth={fc.get('prune_depth', 3)}"]
    if ctx.gnupghome:
        cmd.append(f"--gpg-homedir={ctx.gnupghome}")
    if ctx.cfg.get("icon"):
        cmd.append(f"--icon={ctx.url(ctx.cfg['icon'])}")
    if fc.get("static_deltas"):
        cmd.append("--generate-static-deltas")
    run(cmd + [repo])


def ensure_ostree_dirs(repo: Path) -> None:
    # git keeps no empty directories, and build-update-repo stops at opendir(refs/remotes) without them
    for d in ("refs/heads", "refs/remotes", "refs/mirrors", "tmp"):
        (repo / d).mkdir(parents=True, exist_ok=True)


def cmd_publish_flatpak(ctx: Ctx, args) -> None:
    need("flatpak", "ostree")
    ctx.require_site()
    ctx.check_signing_key()
    app = ctx.app(args.app, "flatpak")
    fp = app["formats"]["flatpak"]
    src = Path(args.src_repo).expanduser().resolve()
    if not (src / "config").exists():
        die(f"{src} is not an OSTree repository (flatpak-builder --repo=…)")
    repo = ctx.site / "flatpak" / "repo"
    if not (repo / "config").exists():
        repo.mkdir(parents=True, exist_ok=True)
        run(["ostree", "init", "--mode=archive-z2", f"--repo={repo}"])
    ensure_ostree_dirs(repo)
    refs = flatpak_refs(src, fp["app_id"], args.arch, args.branch, args.with_debug)
    subject = args.subject or f"{app['name']} {args.version}" + (f" ({args.commit})" if args.commit else "")
    for ref in refs:
        cmd = ["flatpak", "build-commit-from", f"--src-repo={src}", f"--gpg-sign={ctx.gpg_key}",
               f"--subject={subject}", "--no-update-summary"]
        if ctx.gnupghome:
            cmd.append(f"--gpg-homedir={ctx.gnupghome}")
        run(cmd + [repo, ref])
    flatpak_update_repo(ctx, repo)
    app_refs = [r for r in refs if r.startswith("app/")]
    entry = common_entry(args, args.version)
    entry.update({"app_id": fp["app_id"], "refs": refs,
                  "arches": sorted({r.split("/")[2] for r in app_refs}),
                  "branch": app_refs[0].split("/")[3]})
    ctx.record(app["slug"], "flatpak", entry)
    build_site(ctx)
    info(f"published {len(refs)} ref(s) for {fp['app_id']}")
    maybe_push(ctx, args)


# ------------------------------------------------------------------------------------------------------------- apt

def deb_fields(path: Path) -> dict:
    text = out(["dpkg-deb", "-f", path, "Package", "Version", "Architecture"])
    fields = dict(l.split(": ", 1) for l in text.strip().splitlines() if ": " in l)
    for k in ("Package", "Version", "Architecture"):
        if k not in fields:
            die(f"{path}: no {k} field")
    return fields


def deb_compare(a: str, b: str) -> int:
    if a == b:
        return 0
    lt = subprocess.run(["dpkg", "--compare-versions", a, "lt", b]).returncode == 0
    return -1 if lt else 1


def apt_regenerate(ctx: Ctx) -> None:
    ac = ctx.cfg["apt"]
    apt = ctx.site / "apt"
    suite, comp = ac["suite"], ac["component"]
    dist = apt / "dists" / suite
    (apt / "pool" / comp).mkdir(parents=True, exist_ok=True)
    for arch in ac["architectures"]:
        d = dist / comp / f"binary-{arch}"
        d.mkdir(parents=True, exist_ok=True)
        # -a ARCH keeps *_ARCH.deb and *_all.deb; the publisher names every file that way
        packages = out(["apt-ftparchive", "-a", arch, "packages", f"pool/{comp}"], cwd=apt)
        (d / "Packages").write_text(packages)
        with open(d / "Packages.gz", "wb") as f, gzip.GzipFile(fileobj=f, mode="wb", mtime=0) as gz:
            gz.write(packages.encode())
    for name in ("Release", "InRelease", "Release.gpg"):
        (dist / name).unlink(missing_ok=True)
    o = "APT::FTPArchive::Release::"
    release = out(["apt-ftparchive",
                   "-o", f"{o}Origin={ctx.cfg['name']}", "-o", f"{o}Label={ctx.cfg['name']}",
                   "-o", f"{o}Suite={suite}", "-o", f"{o}Codename={suite}",
                   "-o", f"{o}Architectures={' '.join(ac['architectures'])}", "-o", f"{o}Components={comp}",
                   "-o", f"{o}Description={ctx.cfg['description']}",
                   "release", f"dists/{suite}"], cwd=apt)
    (dist / "Release").write_text(release)
    g = ["gpg", "--batch", "--yes", *ctx.gpg_args(), "--local-user", ctx.gpg_key]
    run(g + ["--clearsign", "-o", dist / "InRelease", dist / "Release"])
    run(g + ["--armor", "--detach-sign", "-o", dist / "Release.gpg", dist / "Release"])


def cmd_publish_deb(ctx: Ctx, args) -> None:
    need("dpkg-deb", "dpkg", "apt-ftparchive", "gpg")
    ctx.require_site()
    ctx.check_signing_key()
    app = ctx.app(args.app, "deb")
    comp = ctx.cfg["apt"]["component"]
    pool = ctx.site / "apt" / "pool" / comp / app["slug"]
    pool.mkdir(parents=True, exist_ok=True)
    published = []
    for f in map(Path, args.files):
        if f.suffix != ".deb" or not f.is_file():
            die(f"{f}: not a .deb file")
        if f.stat().st_size >= GITHUB_FILE_LIMIT:
            die(f"{f}: {human(f.stat().st_size)} is over GitHub's 100 MB file limit")
        fld = deb_fields(f)
        arch = fld["Architecture"]
        if arch != "all" and arch not in ctx.cfg["apt"]["architectures"]:
            die(f"{f}: architecture {arch} is not in config.json apt.architectures")
        name = f"{fld['Package']}_{fld['Version'].split(':', 1)[-1]}_{arch}.deb"
        shutil.copyfile(f, pool / name)
        published.append({"package": fld["Package"], "version": fld["Version"], "arch": arch,
                          "file": f"apt/pool/{comp}/{app['slug']}/{name}"})
    # keep the newest N versions of each package and architecture
    keep = ctx.cfg["apt"].get("keep_versions", 2)
    groups: dict[tuple, list] = {}
    for deb in pool.glob("*.deb"):
        fld = deb_fields(deb)
        groups.setdefault((fld["Package"], fld["Architecture"]), []).append((fld["Version"], deb))
    for items in groups.values():
        items.sort(key=functools.cmp_to_key(lambda a, b: deb_compare(a[0], b[0])), reverse=True)
        for _, old in items[keep:]:
            info(f"pruning {old.name}")
            old.unlink()
    apt_regenerate(ctx)
    wanted = app["formats"]["deb"].get("package")
    for p in published:
        if wanted and p["package"] != wanted:
            info(f"warning: {p['file']} is package '{p['package']}', catalog says '{wanted}'")
    entry = common_entry(args, args.version or published[0]["version"])
    entry["packages"] = published
    ctx.record(app["slug"], "deb", entry)
    build_site(ctx)
    maybe_push(ctx, args)


# --------------------------------------------------------------------------------------------------------- android

def fdroid_env() -> dict:
    ks = os.environ.get("ICESUITE_FDROID_KEYSTORE")
    pw = os.environ.get("ICESUITE_FDROID_KEYSTORE_PASS")
    if not ks or not pw:
        die("set ICESUITE_FDROID_KEYSTORE and ICESUITE_FDROID_KEYSTORE_PASS (see 'icesuite.py fdroid-keygen')")
    ks = Path(ks).expanduser().resolve()
    if not ks.exists():
        die(f"keystore {ks} not found")
    return {"keystore": str(ks), "keystorepass": pw, "keypass": os.environ.get("ICESUITE_FDROID_KEY_PASS", pw)}


def fdroid_fingerprint(ctx: Ctx, env: dict) -> str:
    cert = subprocess.run(["keytool", "-exportcert", "-alias", ctx.cfg["fdroid"]["key_alias"],
                           "-keystore", env["keystore"], "-storepass", env["keystorepass"]],
                          check=True, capture_output=True).stdout
    return hashlib.sha256(cert).hexdigest().upper()


def fdroid_metadata(ctx: Ctx, app: dict) -> str:
    lines = [f"Name: {json.dumps(app['name'])}", f"Summary: {json.dumps(app['summary'][:80])}",
             f"Description: {json.dumps(app.get('description') or app['summary'])}",
             f"WebSite: {app['homepage']}", f"SourceCode: {app['homepage']}"]
    if app.get("issues"):
        lines.append(f"IssueTracker: {app['issues']}")
    if app.get("changelog"):
        lines.append(f"Changelog: {app['changelog']}")
    if app.get("license"):
        lines.append(f"License: {app['license']}")
    lines.append("Categories:")
    lines += [f"  - {c}" for c in (app.get("categories") or ["System"])]
    return "\n".join(lines) + "\n"


def fdroid_update(ctx: Ctx, work: Path, env: dict) -> None:
    cfg = {
        "repo_url": ctx.url("fdroid/repo"),
        "repo_name": ctx.cfg["name"],
        "repo_description": ctx.cfg["description"],
        "archive_older": 0,
        "repo_keyalias": ctx.cfg["fdroid"]["key_alias"],
        "keydname": f"CN={ctx.cfg['name']}",
        **env,
    }
    conf = work / "config.yml"
    conf.write_text("".join(f"{k}: {json.dumps(v)}\n" for k, v in cfg.items()))
    conf.chmod(0o600)
    run(["fdroid", "update", "--rename-apks", "--delete-unknown"], cwd=work)


def cmd_publish_apk(ctx: Ctx, args) -> None:
    need("fdroid", "keytool")
    ctx.require_site()
    env = fdroid_env()
    app = ctx.app(args.app, "android")
    package = app["formats"]["android"].get("package")
    if not package:
        die(f"catalog/{app['slug']}.json: formats.android.package (the application ID) is required")
    site_fd = ctx.site / "fdroid"
    keep = ctx.cfg["fdroid"].get("keep_versions", 3)
    with tempfile.TemporaryDirectory(prefix="icesuite-fdroid-") as tmp:
        work = Path(tmp)
        for sub in ("repo", "metadata"):
            if (site_fd / sub).exists():
                shutil.copytree(site_fd / sub, work / sub)
            (work / sub).mkdir(exist_ok=True)
        for f in map(Path, args.files):
            if f.suffix != ".apk" or not f.is_file():
                die(f"{f}: not an .apk file")
            if f.stat().st_size >= GITHUB_FILE_LIMIT:
                die(f"{f}: {human(f.stat().st_size)} is over GitHub's 100 MB file limit")
            shutil.copyfile(f, work / "repo" / f.name)
        (work / "metadata" / f"{package}.yml").write_text(fdroid_metadata(ctx, app))
        fdroid_update(ctx, work, env)
        index = read_json(work / "repo" / "index-v1.json", {})
        versions = sorted(index.get("packages", {}).get(package, []), key=lambda p: p["versionCode"], reverse=True)
        if not versions:
            die(f"F-Droid found no APK for {package}; check the APK's application ID against the catalog")
        if len(versions) > keep:
            for old in versions[keep:]:
                info(f"pruning {old['apkName']}")
                (work / "repo" / old["apkName"]).unlink(missing_ok=True)
            fdroid_update(ctx, work, env)
        for sub in ("repo", "metadata"):
            shutil.rmtree(site_fd / sub, ignore_errors=True)
            shutil.copytree(work / sub, site_fd / sub)
    newest = versions[0]
    write_json(ctx.site / "meta" / "fdroid.json", {"fingerprint": fdroid_fingerprint(ctx, env)})
    entry = common_entry(args, args.version or newest.get("versionName"))
    entry.update({"package": package, "version_code": newest["versionCode"],
                  "file": f"fdroid/repo/{newest['apkName']}", "size": newest.get("size"),
                  "sha256": newest.get("hash")})
    ctx.record(app["slug"], "android", entry)
    build_site(ctx)
    maybe_push(ctx, args)


def cmd_fdroid_keygen(ctx: Ctx, args) -> None:
    need("keytool")
    d = Path(args.dir).expanduser().resolve()
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    ks = d / "keystore.p12"
    if ks.exists():
        die(f"{ks} exists; refusing to replace a signing key")
    pw = base64.urlsafe_b64encode(os.urandom(24)).decode()
    run(["keytool", "-genkeypair", "-keystore", ks, "-storetype", "PKCS12", "-alias", ctx.cfg["fdroid"]["key_alias"],
         "-keyalg", "RSA", "-keysize", "4096", "-validity", "10000", "-dname", f"CN={ctx.cfg['name']}",
         f"-storepass:file", _secret_file(d, pw)])
    (d / "env").write_text(f"export ICESUITE_FDROID_KEYSTORE={ks}\nexport ICESUITE_FDROID_KEYSTORE_PASS={pw}\n")
    (d / "env").chmod(0o600)
    (d / ".pass").unlink()
    info(f"created {ks}; load it with: . {d / 'env'}   (back up the whole folder: losing it means a new repo key)")


def _secret_file(d: Path, pw: str) -> Path:
    p = d / ".pass"
    p.write_text(pw)
    p.chmod(0o600)
    return p


# --------------------------------------------------------------------------------------------------------- windows

def gh_json(cmd: list[str]):
    return json.loads(out(["gh", *cmd]) or "[]")


def cmd_publish_windows(ctx: Ctx, args) -> None:
    ctx.require_site()
    app = ctx.app(args.app, "windows")
    repo = ctx.cfg["github_repo"]
    files = [Path(f) for f in args.files]
    for f in files:
        if not f.is_file():
            die(f"{f}: no such file")
        if f.suffix.lower() not in (".exe", ".msi", ".msix", ".zip"):
            die(f"{f}: expected .exe, .msi, .msix or .zip")
        if f.stat().st_size >= 2 * 1024 ** 3:
            die(f"{f}: GitHub release assets must be under 2 GB")
    tag = f"{app['slug']}-v{safe_tag(args.version)}"
    title = f"{app['name']} {args.version}" + (f" ({args.commit})" if args.commit else "")
    notes = args.notes or f"Test build of {app['name']}. Not a release; see {ctx.url()}"
    if args.dry_run:
        info(f"dry run: would create pre-release {tag} on {repo} with {', '.join(f.name for f in files)}")
    else:
        need("gh")
        exists = subprocess.run(["gh", "release", "view", tag, "--repo", repo], capture_output=True).returncode == 0
        if exists:
            run(["gh", "release", "upload", tag, *files, "--repo", repo, "--clobber"])
        else:
            run(["gh", "release", "create", tag, *files, "--repo", repo, "--prerelease", "--title", title,
                 "--notes", notes, "--target", ctx.cfg.get("source_branch", "main")])
        keep = ctx.cfg["windows"].get("keep_releases", 3)
        rels = [r for r in gh_json(["release", "list", "--repo", repo, "--limit", "500",
                                    "--json", "tagName,createdAt"]) if r["tagName"].startswith(app["slug"] + "-v")]
        rels.sort(key=lambda r: r["createdAt"], reverse=True)
        for r in rels[keep:]:
            run(["gh", "release", "delete", r["tagName"], "--repo", repo, "--cleanup-tag", "--yes"])
    base = f"https://github.com/{repo}/releases/download/{tag}/"
    entry = common_entry(args, args.version)
    entry.update({"tag": tag, "release_url": f"https://github.com/{repo}/releases/tag/{tag}",
                  "files": [{"name": f.name, "url": base + f.name, "size": f.stat().st_size, "sha256": sha256(f)}
                            for f in files]})
    write_json(ctx.site / "windows" / f"{app['slug']}.json", {"app": app["slug"], "name": app["name"], **entry})
    ctx.record(app["slug"], "windows", entry)
    build_site(ctx)
    maybe_push(ctx, args)


# ------------------------------------------------------------------------------------------------------------- git

def git(ctx: Ctx, *a, **kw):
    return run(["git", "-C", ctx.site, *a], **kw)


def git_out(ctx: Ctx, *a) -> str:
    return out(["git", "-C", ctx.site, *a]).strip()


def remote_url(ctx: Ctx) -> str:
    return os.environ.get("ICESUITE_REMOTE") or f"https://github.com/{ctx.cfg['github_repo']}.git"


def cmd_sync(ctx: Ctx, args) -> None:
    branch = ctx.cfg["pages_branch"]
    if not (ctx.site / ".git").exists():
        ctx.site.parent.mkdir(parents=True, exist_ok=True)
        run(["git", "clone", "--depth", "1", "--single-branch", "--branch", branch, remote_url(ctx), ctx.site])
        return
    dirty = git_out(ctx, "status", "--porcelain")
    if dirty and not args.force:
        die(f"{ctx.site} has unpushed changes; run 'icesuite.py push' first, or 'sync --force' to discard them")
    git(ctx, "fetch", "--depth", "1", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}")
    git(ctx, "checkout", "-q", "-B", branch, f"refs/remotes/origin/{branch}")
    git(ctx, "reset", "-q", "--hard", f"refs/remotes/origin/{branch}")
    git(ctx, "clean", "-q", "-fdx")


def check_limits(ctx: Ctx) -> None:
    total, big = 0, []
    for p in ctx.site.rglob("*"):
        if ".git" in p.relative_to(ctx.site).parts[:1] or not p.is_file() or p.is_symlink():
            continue
        if "/tmp/" in f"/{p.relative_to(ctx.site).as_posix()}" and "flatpak/repo" in p.as_posix():
            continue
        size = p.stat().st_size
        total += size
        if size >= GITHUB_FILE_LIMIT:
            big.append(f"{p.relative_to(ctx.site)} ({human(size)})")
    if big:
        die("files over GitHub's 100 MB limit: " + ", ".join(big))
    limit = ctx.cfg.get("site_limit_mb", 950) * 1024 * 1024
    info(f"site size {human(total)} of {human(limit)}")
    if total > limit:
        die("the site is over the size budget; lower flatpak.prune_depth, apt.keep_versions or fdroid.keep_versions")
    if total > 0.8 * limit:
        info("warning: the site is over 80% of its size budget (GitHub Pages sites are limited to 1 GB)")


def push_message(ctx: Ctx) -> str:
    lines = [f"{ctx.cfg['name']}: published builds"]
    hist = read_json(ctx.site / "meta" / "history.json", [])
    if hist:
        h = hist[0]
        lines[0] = f"Publish {h['app']} {h['format']} {h.get('version') or ''}".rstrip()
    lines.append("")
    for slug, fmts in sorted(ctx.builds().items()):
        for fmt, e in sorted(fmts.items()):
            lines.append(f"- {slug} {fmt}: {e.get('version')}" + (f" ({e['commit']})" if e.get("commit") else ""))
    return "\n".join(lines) + "\n"


def cmd_push(ctx: Ctx, args) -> None:
    ctx.require_site()
    branch = ctx.cfg["pages_branch"]
    check_limits(ctx)
    git(ctx, "add", "-A")
    has_head = subprocess.run(["git", "-C", ctx.site, "rev-parse", "-q", "--verify", "HEAD"],
                              capture_output=True).returncode == 0
    if has_head and subprocess.run(["git", "-C", ctx.site, "diff", "--cached", "--quiet", "HEAD"]).returncode == 0:
        info("nothing to publish")
        return
    tree = git_out(ctx, "write-tree")
    msg = args.message or push_message(ctx)
    # a parentless commit: the Pages branch holds only the current site, so its history never grows
    commit = out(["git", "-C", ctx.site, "commit-tree", tree], input=msg).strip()
    base = subprocess.run(["git", "-C", ctx.site, "rev-parse", "-q", "--verify", f"refs/remotes/origin/{branch}"],
                          capture_output=True, text=True).stdout.strip()
    lease = f"--force-with-lease=refs/heads/{branch}:{base}"
    try:
        git(ctx, "-c", "push.negotiate=true", "push", lease, "origin", f"{commit}:refs/heads/{branch}")
    except subprocess.CalledProcessError:
        die("push refused: someone published since your last sync. Run 'sync --force', publish again, then push")
    git(ctx, "update-ref", f"refs/remotes/origin/{branch}", commit)
    git(ctx, "checkout", "-q", "-B", branch, commit)
    info(f"pushed {commit[:12]} to {branch}; Pages redeploys in about a minute: {ctx.url()}")


def maybe_push(ctx: Ctx, args) -> None:
    if getattr(args, "push", False):
        cmd_push(ctx, argparse.Namespace(message=None))
    else:
        info("site updated locally; run 'icesuite.py push' to publish")


def cmd_check(ctx: Ctx, args) -> None:
    ctx.require_site()
    check_limits(ctx)
    for slug, fmts in sorted(ctx.builds().items()):
        for fmt, e in sorted(fmts.items()):
            print(f"{slug:24} {fmt:8} {e.get('version') or '':32} {e.get('commit') or '':10} {e['date']}")


# ------------------------------------------------------------------------------------------------------------ site

def flatpakrepo_text(ctx: Ctx) -> str:
    fc = ctx.cfg["flatpak"]
    lines = ["[Flatpak Repo]", f"Title={ctx.cfg['name']}", f"Url={ctx.url('flatpak/repo/')}",
             f"Homepage={ctx.url()}", f"Comment={ctx.cfg['description']}", f"Description={ctx.cfg['description']}"]
    if ctx.cfg.get("icon"):
        lines.append(f"Icon={ctx.url(ctx.cfg['icon'])}")
    lines += [f"GPGKey={base64.b64encode(ctx.public_key.read_bytes()).decode()}",
              f"DefaultBranch={fc['default_branch']}", f"RuntimeRepo={fc['runtime_repo']}"]
    return "\n".join(lines) + "\n"


def flatpakref_text(ctx: Ctx, app: dict, build: dict) -> str:
    fc = ctx.cfg["flatpak"]
    lines = ["[Flatpak Ref]", f"Name={build['app_id']}", f"Branch={build.get('branch') or fc['default_branch']}",
             f"Title={app['name']} (test)", f"Url={ctx.url('flatpak/repo/')}", f"Homepage={app['homepage']}",
             f"Comment={app['summary']}"]
    if app.get("icon"):
        lines.append(f"Icon={ctx.url('icons/' + Path(app['icon']).name)}")
    lines += ["IsRuntime=false", f"GPGKey={base64.b64encode(ctx.public_key.read_bytes()).decode()}",
              f"RuntimeRepo={fc['runtime_repo']}", f"SuggestRemoteName={fc['remote_name']}"]
    return "\n".join(lines) + "\n"


def build_site(ctx: Ctx) -> None:
    s = ctx.site
    s.mkdir(parents=True, exist_ok=True)
    (s / ".nojekyll").write_text("")
    (s / ".gitignore").write_text("flatpak/repo/.lock\nflatpak/repo/tmp/\n")
    (s / "keys").mkdir(exist_ok=True)
    shutil.copyfile(ctx.public_key, s / "keys" / ctx.key_name)
    (s / "icons").mkdir(exist_ok=True)
    for app in ctx.catalog.values():
        if app.get("icon"):
            shutil.copyfile(ctx.base / app["icon"], s / "icons" / Path(app["icon"]).name)
    if ctx.cfg.get("icon"):
        shutil.copyfile(ctx.base / ctx.cfg["icon"], s / ctx.cfg["icon"])
    builds = ctx.builds()
    fp_dir = s / "flatpak"
    if (fp_dir / "repo" / "config").exists():
        (fp_dir / f"{ctx.cfg['flatpak']['remote_name']}.flatpakrepo").write_text(flatpakrepo_text(ctx))
        for slug, b in builds.items():
            if "flatpak" in b and slug in ctx.catalog:
                (fp_dir / f"{b['flatpak']['app_id']}.flatpakref").write_text(
                    flatpakref_text(ctx, ctx.catalog[slug], b["flatpak"]))
    (s / "README.md").write_text(
        f"# {ctx.cfg['name']}\n\nThis branch is generated by `tools/icesuite.py` on the "
        f"`{ctx.cfg.get('source_branch', 'main')}` branch and is replaced on every publish. "
        f"Install instructions: <{ctx.url()}>\n")
    (s / "index.html").write_text(render_index(ctx, builds))


# ------------------------------------------------------------------------------------------------------------ page

CSS = """
:root{--bg:#f6f8fa;--fg:#1f2328;--muted:#59636e;--card:#fff;--line:#d1d9e0;--code:#eef1f4;--accent:#1b6fd6;--tag:#e7f0fb}
@media (prefers-color-scheme:dark){:root{--bg:#0d1117;--fg:#e6edf3;--muted:#9198a1;--card:#151b23;--line:#30363d;--code:#1f2630;--accent:#4c9bf0;--tag:#16263a}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:860px;margin:0 auto;padding:32px 16px 64px}
h1{font-size:1.7rem;margin:0 0 4px}
h2{font-size:1.25rem;margin:40px 0 8px;padding-bottom:6px;border-bottom:1px solid var(--line)}
h3{font-size:1.05rem;margin:20px 0 6px}
.muted{color:var(--muted)}
.note{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--accent);border-radius:8px;padding:12px 16px;margin:16px 0}
.app{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 20px;margin:16px 0}
.app header{display:flex;gap:14px;align-items:center}
.app header img{width:56px;height:56px}
.app header h3{margin:0;font-size:1.2rem}
.tags{display:flex;flex-wrap:wrap;gap:6px;margin:10px 0 4px}
.tag{background:var(--tag);border-radius:999px;padding:2px 10px;font-size:.85rem}
details{border-top:1px solid var(--line);padding:8px 0}
details:last-of-type{border-bottom:1px solid var(--line)}
summary{cursor:pointer;font-weight:600}
summary .muted{font-weight:400}
pre{background:var(--code);border:1px solid var(--line);border-radius:8px;padding:12px 14px;overflow-x:auto;margin:8px 0 12px}
code{font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
p code,li code,td code{background:var(--code);padding:1px 5px;border-radius:4px}
a{color:var(--accent)}
nav a{margin-right:14px}
table{border-collapse:collapse;width:100%;font-size:.95rem}
td,th{text-align:left;padding:4px 8px;border-bottom:1px solid var(--line)}
.btn{display:inline-block;background:var(--accent);color:#fff;text-decoration:none;border-radius:8px;padding:6px 14px;margin:4px 8px 4px 0}
""".strip()


def e(s) -> str:
    return html.escape(str(s or ""), quote=True)


def pre(text: str) -> str:
    return f"<pre><code>{e(text)}</code></pre>"


def short_date(iso: str) -> str:
    return (iso or "")[:10]


def label(b: dict) -> str:
    v = e(b.get("version"))
    if b.get("commit"):
        v += f" <span class=\"muted\">· {e(b['commit'])}</span>"
    return v + f" <span class=\"muted\">· {short_date(b.get('date'))}</span>"


def public_key_fingerprint(ctx: Ctx) -> str | None:
    if not shutil.which("gpg"):
        return None
    try:
        pub = out(["gpg", "--batch", "--with-colons", "--show-keys", ctx.public_key])
    except subprocess.CalledProcessError:
        return None
    return next((l.split(":")[9] for l in pub.splitlines() if l.startswith("fpr:")), None)


def render_index(ctx: Ctx, builds: dict) -> str:
    cfg, fc, ac = ctx.cfg, ctx.cfg["flatpak"], ctx.cfg["apt"]
    remote = fc["remote_name"]
    key_url = ctx.url(f"keys/{ctx.key_name}")
    fdroid_meta = read_json(ctx.site / "meta" / "fdroid.json", {})
    have = {fmt for b in builds.values() for fmt in b}
    apps = [a for a in ctx.catalog.values() if builds.get(a["slug"])]
    apps += [a for a in ctx.catalog.values() if not builds.get(a["slug"])]

    parts = [f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(cfg['name'])}</title>
{f'<link rel="icon" href="{e(cfg["icon"])}">' if cfg.get('icon') else ''}
<style>
{CSS}
</style>
</head>
<body>
<main>
<h1>{e(cfg['name'])}</h1>
<div class="muted">{e(cfg['description'])}</div>
<div class="note">These are test builds, checked in a virtual lab before they reach testers' hardware. Expect rough
edges, and report problems in each app's issue tracker. Every build on an app's <code>main</code> branch can land here.</div>
<nav>{''.join(f'<a href="#{e(a["slug"])}">{e(a["name"])}</a>' for a in apps)}<a href="#setup">Set up</a><a href="#keys">Signing keys</a></nav>
<h2 id="apps">Apps</h2>
"""]

    for app in apps:
        slug, b = app["slug"], builds.get(app["slug"], {})
        icon = f'<img src="icons/{e(Path(app["icon"]).name)}" alt="">' if app.get("icon") else ""
        links = [f'<a href="{e(app["homepage"])}">Source</a>']
        for k, t in (("issues", "Issues"), ("changelog", "Changes"), ("docs", "Docs")):
            if app.get(k):
                links.append(f'<a href="{e(app[k])}">{t}</a>')
        tags = "".join(f'<span class="tag">{t}</span>' for f, t in
                       (("flatpak", "Flatpak"), ("deb", "Debian / Ubuntu"), ("android", "Android"),
                        ("windows", "Windows")) if f in b)
        parts.append(f"""<section class="app" id="{e(slug)}">
<header>{icon}<div><h3>{e(app['name'])}</h3><div class="muted">{e(app['summary'])}</div>
<div>{' · '.join(links)}</div></div></header>
<div class="tags">{tags or '<span class="muted">No builds published yet.</span>'}</div>
""")
        if "flatpak" in b:
            fb = b["flatpak"]
            app_id = fb["app_id"]
            flathub = "" if not app["formats"].get("flatpak", {}).get("needs_flathub", True) else \
                " Needs the Flathub remote for its runtime (<a href=\"#setup-flatpak\">set up</a>)."
            parts.append(f"""<details open><summary>Flatpak (Linux) <span class="muted">{label(fb)} · {e(', '.join(fb.get('arches', [])))}</span></summary>
<p>With the <code>{e(remote)}</code> remote added (<a href="#setup-flatpak">once</a>):{flathub}</p>
{pre(f'flatpak install --user {remote} {app_id}')}
<p class="muted">Or in one step: <code>flatpak install --user {e(ctx.url(f'flatpak/{app_id}.flatpakref'))}</code>.
GNOME Software and KDE Discover also open the <a href="flatpak/{e(app_id)}.flatpakref">.flatpakref</a>.
Run with <code>flatpak run {e(app_id)}</code>; update with <code>flatpak update</code>.</p>
</details>
""")
        if "deb" in b:
            db = b["deb"]
            pkgs = sorted({p["package"] for p in db.get("packages", [])}) or [app["formats"]["deb"].get("package")]
            files = "".join(f'<li><a href="{e(p["file"])}">{e(Path(p["file"]).name)}</a></li>'
                            for p in db.get("packages", []))
            parts.append(f"""<details><summary>Debian / Ubuntu (.deb) <span class="muted">{label(db)}</span></summary>
<p>With the APT repository added (<a href="#setup-apt">once</a>):</p>
{pre('sudo apt update && sudo apt install ' + ' '.join(pkgs))}
<p class="muted">Updates arrive with <code>sudo apt upgrade</code>. Direct downloads:</p><ul>{files}</ul>
</details>
""")
        if "android" in b:
            ab = b["android"]
            parts.append(f"""<details><summary>Android (.apk) <span class="muted">{label(ab)}</span></summary>
<p>Add the F-Droid repository (<a href="#setup-android">once</a>) in F-Droid, Droid-ify or Neo Store, then search for
<b>{e(app['name'])}</b>. Updates arrive through the store app.</p>
<p><a class="btn" href="{e(ab['file'])}">Download APK</a> <span class="muted">{e(Path(ab['file']).name)}
{('· ' + human(ab['size'])) if ab.get('size') else ''}</span></p>
<p class="muted">SHA-256 <code>{e(ab.get('sha256'))}</code></p>
</details>
""")
        if "windows" in b:
            wb = b["windows"]
            btns = "".join(f'<a class="btn" href="{e(f["url"])}">{e(f["name"])}</a>' for f in wb.get("files", []))
            sums = "".join(f'<tr><td><code>{e(f["name"])}</code></td><td>{human(f["size"])}</td>'
                           f'<td><code>{e(f["sha256"])}</code></td></tr>' for f in wb.get("files", []))
            parts.append(f"""<details><summary>Windows <span class="muted">{label(wb)}</span></summary>
<p>{btns}</p>
<p class="muted">Test installers are not code-signed: Windows SmartScreen shows “Windows protected your PC”. Choose
<i>More info → Run anyway</i>. Check the download first:</p>
<table><tr><th>File</th><th>Size</th><th>SHA-256</th></tr>{sums}</table>
<p class="muted"><a href="{e(wb['release_url'])}">Release page</a> · machine-readable feed:
<a href="windows/{e(slug)}.json">windows/{e(slug)}.json</a></p>
</details>
""")
        if app.get("notes") and b:
            # notes come from the catalog, which the maintainer writes: simple HTML is allowed
            parts.append("<ul>" + "".join(f"<li>{n}</li>" for n in app["notes"]) + "</ul>\n")
        parts.append("</section>\n")

    parts.append('<h2 id="setup">Set up</h2>\n<p>Each platform needs its repository added once. After that, every '
                 'Icesuite app installs and updates from it.</p>\n')
    if "flatpak" in have:
        parts.append(f"""<h3 id="setup-flatpak">Flatpak</h3>
{pre(f'''flatpak remote-add --user --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
flatpak remote-add --user --if-not-exists {remote} {ctx.url(f'flatpak/{remote}.flatpakrepo')}''')}
<p class="muted">Flathub supplies the runtimes (KDE, GNOME, freedesktop) the apps use. Remove later with
<code>flatpak remote-delete --user {e(remote)}</code> (uninstall its apps first).</p>
<p><b>Had the old <code>icefin-test</code> remote?</b> Point it here, or replace it:</p>
{pre(f'''flatpak remote-modify --user --url={ctx.url('flatpak/repo/')} icefin-test
# or: flatpak remote-delete --force --user icefin-test && flatpak remote-add --user {remote} {ctx.url(f'flatpak/{remote}.flatpakrepo')}''')}
""")
    if "deb" in have:
        kr = f"/etc/apt/keyrings/{ctx.key_name}"
        parts.append(f"""<h3 id="setup-apt">Debian / Ubuntu (APT)</h3>
<p>Debian 12 or newer, Ubuntu 22.04 or newer; {e(', '.join(ac['architectures']))}.</p>
{pre(f'''sudo install -d -m 0755 /etc/apt/keyrings
sudo curl -fsSLo {kr} {key_url}
echo "deb [signed-by={kr}] {ctx.url('apt')} {ac['suite']} {ac['component']}" | sudo tee /etc/apt/sources.list.d/{remote}.list
sudo apt update''')}
""")
    if "android" in have:
        fpr = fdroid_meta.get("fingerprint", "")
        add = ctx.url("fdroid/repo") + (f"?fingerprint={fpr}" if fpr else "")
        parts.append(f"""<h3 id="setup-android">Android (F-Droid repository)</h3>
<p>Open this link on the phone, or in F-Droid go to <i>Settings → Repositories → +</i> and paste it:</p>
{pre(add)}
<p class="muted">Android asks once to allow installs from the store app. Repository fingerprint (SHA-256):
<code>{e(fpr)}</code></p>
""")
    if "windows" in have:
        parts.append("""<h3 id="setup-windows">Windows</h3>
<p>No repository to add: download the installer from the app above. Installers are pre-releases on this
repository's <a href="https://github.com/%s/releases">Releases page</a>.</p>
""" % e(cfg["github_repo"]))

    fpr_gpg = public_key_fingerprint(ctx) or cfg["gpg"]["fingerprint"]
    fpr_fmt = " ".join(fpr_gpg[i:i + 4] for i in range(0, len(fpr_gpg), 4))
    parts.append(f"""<h2 id="keys">Signing keys</h2>
<p>Flatpak commits and the APT repository are signed with one OpenPGP key, embedded in the <code>.flatpakrepo</code>
and published as <a href="keys/{e(ctx.key_name)}">{e(ctx.key_name)}</a>:</p>
{pre(fpr_fmt)}
{'<p>The F-Droid index is signed with its own key; fingerprint above under Android.</p>' if 'android' in have else ''}
<p class="muted">Generated {e(now())} · <a href="https://github.com/{e(cfg['github_repo'])}">source</a></p>
</main>
</body>
</html>
""")
    return "".join(parts)


def cmd_build_site(ctx: Ctx, args) -> None:
    ctx.require_site()
    build_site(ctx)
    info(f"wrote {ctx.site / 'index.html'}")


# ------------------------------------------------------------------------------------------------------------- cli

def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="icesuite.py", description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", help="config file (default: config.json next to tools/)")
    p.add_argument("--catalog", help="catalog directory (default: catalog/ next to the config)")
    p.add_argument("--site", help="Pages branch checkout (default: $ICESUITE_SITE or ~/icesuite-site)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def publish(name, fn, help_):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--app", required=True, help="catalog slug, e.g. icefin-player")
        sp.add_argument("--commit", help="source commit, shown on the page")
        sp.add_argument("--notes", help="one-line note for this build")
        sp.add_argument("--push", action="store_true", help="push the site afterwards")
        sp.set_defaults(fn=fn)
        return sp

    s = sub.add_parser("sync", help="clone or refresh the Pages branch checkout")
    s.add_argument("--force", action="store_true", help="discard unpushed local changes")
    s.set_defaults(fn=cmd_sync)

    s = publish("publish-flatpak", cmd_publish_flatpak, "add an app from a flatpak-builder repo")
    s.add_argument("--src-repo", required=True, help="OSTree repo from flatpak-builder --repo=")
    s.add_argument("--version", required=True)
    s.add_argument("--arch", action="append", help="only this arch (repeatable; default: all in the source repo)")
    s.add_argument("--branch", help="only this branch (default: all in the source repo)")
    s.add_argument("--with-debug", action="store_true", help="also publish the .Debug extension")
    s.add_argument("--subject", help="commit subject (default: '<name> <version> (<commit>)')")

    s = publish("publish-deb", cmd_publish_deb, "add .deb packages to the APT repository")
    s.add_argument("--version", help="label for the page (default: the first package's version)")
    s.add_argument("files", nargs="+")

    s = publish("publish-apk", cmd_publish_apk, "add .apk packages to the F-Droid repository")
    s.add_argument("--version", help="label for the page (default: versionName)")
    s.add_argument("files", nargs="+")

    s = publish("publish-windows", cmd_publish_windows, "upload Windows installers as a GitHub pre-release")
    s.add_argument("--version", required=True)
    s.add_argument("--dry-run", action="store_true", help="skip GitHub; only update the site")
    s.add_argument("files", nargs="+")

    s = sub.add_parser("fdroid-keygen", help="create the F-Droid repository signing keystore (once)")
    s.add_argument("--dir", default="~/icesuite-fdroid-key")
    s.set_defaults(fn=cmd_fdroid_keygen)

    sub.add_parser("build-site", help="regenerate index.html and the repo files").set_defaults(fn=cmd_build_site)

    s = sub.add_parser("push", help="commit the site as one parentless commit and force-push it")
    s.add_argument("-m", "--message")
    s.set_defaults(fn=cmd_push)

    sub.add_parser("check", help="size budget and what is published").set_defaults(fn=cmd_check)

    args = p.parse_args(argv)
    ctx = Ctx(args)
    try:
        args.fn(ctx, args)
    except subprocess.CalledProcessError as exc:
        err = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        die(f"command failed ({exc.returncode}): {' '.join(_redact([str(c) for c in exc.cmd]))}\n{err}")


if __name__ == "__main__":
    main()
