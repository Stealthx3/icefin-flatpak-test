#!/usr/bin/env bash
# End-to-end test of tools/icesuite.py with throwaway keys, a local bare "GitHub" remote and file:// URLs.
# Needs: flatpak ostree apt-utils dpkg-dev gnupg git python3 fdroidserver; for the Android part also aapt,
# zipalign, apksigner and an android.jar (Debian/Ubuntu: aapt zipalign apksigner android-sdk-platform-23).
set -euo pipefail
[ -n "${E2E_TRACE:-}" ] && set -x
here=$(cd "$(dirname "$0")/.." && pwd)
T=$(mktemp -d)
[ -n "${E2E_KEEP:-}" ] && echo "keeping $T" || trap 'rm -rf "$T"' EXIT
pass() { printf '  ok  %s\n' "$*"; }
fail() { printf 'FAIL  %s\n' "$*" >&2; exit 1; }

export GNUPGHOME=$T/gnupg HOME=$T/home GIT_AUTHOR_NAME=test GIT_AUTHOR_EMAIL=test@example.invalid
export GIT_COMMITTER_NAME=test GIT_COMMITTER_EMAIL=test@example.invalid
mkdir -p "$GNUPGHOME" "$HOME" && chmod 700 "$GNUPGHOME"
gpg --batch -q --passphrase '' --quick-gen-key "Icesuite e2e test" ed25519 sign 1d
FPR=$(gpg --batch --with-colons --list-keys | awk -F: '/^fpr/{print $10; exit}')
gpg --batch --export "$FPR" > "$T/pub.gpg"
export ICESUITE_GPG_KEY=$FPR ICESUITE_PUBLIC_KEY=$T/pub.gpg

# config and catalog: the real ones, pointed at temp locations
SITE=$T/site
python3 - "$here/config.json" "$T/config.json" "$SITE" <<'EOF'
import json, sys
c = json.load(open(sys.argv[1]))
c["base_url"] = "file://" + sys.argv[3] + "/"
c["fdroid"]["keep_versions"] = 2
json.dump(c, open(sys.argv[2], "w"), indent=2)
EOF
mkdir -p "$T/catalog" "$T/icons" "$T/keys"
cp "$here"/icons/* "$T/icons/"
cat > "$T/catalog/hello.json" <<'EOF'
{"slug": "hello", "name": "Hello Test", "summary": "End-to-end test app", "homepage": "https://example.invalid/hello",
 "license": "MIT", "icon": "icons/icefin-player.svg",
 "formats": {"flatpak": {"app_id": "io.example.Hello", "needs_flathub": false}, "deb": {"package": "hello-test"},
             "android": {"package": "io.example.hello"}, "windows": {}},
 "notes": ["A <b>note</b> from the catalog."]}
EOF
ice() { python3 "$here/tools/icesuite.py" --config "$T/config.json" --site "$SITE" "$@"; }

# a local stand-in for GitHub with an empty gh-pages branch
git init -q --bare "$T/remote.git"
git init -q "$T/seed" && git -C "$T/seed" checkout -q -b gh-pages
echo seed > "$T/seed/README.md" && git -C "$T/seed" add -A && git -C "$T/seed" commit -qm seed
git -C "$T/seed" push -q "$T/remote.git" gh-pages
export ICESUITE_REMOTE=$T/remote.git
ice sync 2>/dev/null && [ -f "$SITE/README.md" ] && pass "sync clones the Pages branch"

echo "Flatpak"
mkdir -p "$T/fp"
# a minimal app directory by hand: build-init would need the SDK installed
mkdir -p "$T/fp/app/files/bin" "$T/fp/app/export"
printf '#!/bin/sh\necho hello\n' > "$T/fp/app/files/bin/hello" && chmod +x "$T/fp/app/files/bin/hello"
printf '[Application]\nname=io.example.Hello\nruntime=org.freedesktop.Platform/x86_64/24.08\nsdk=org.freedesktop.Sdk/x86_64/24.08\ncommand=hello\n' > "$T/fp/app/metadata"
flatpak build-export "$T/fp/src" "$T/fp/app" master >/dev/null 2>&1
ice publish-flatpak --app hello --src-repo "$T/fp/src" --version 1.0.0 --commit abc1234 2>"$T/log" || { cat "$T/log"; fail publish-flatpak; }
ostree refs --repo="$SITE/flatpak/repo" | grep -qx 'app/io.example.Hello/x86_64/master' && pass "app ref committed"
[ -f "$SITE/flatpak/repo/summary.sig" ] && pass "summary signed"
flatpak remote-add --user --if-not-exists e2e "$SITE/flatpak/icesuite-test.flatpakrepo"
flatpak remote-ls --user e2e --columns=application | grep -qx io.example.Hello \
  && pass ".flatpakrepo (embedded key) lists the app with signatures on"
grep -q '^Name=io.example.Hello$' "$SITE/flatpak/io.example.Hello.flatpakref" && pass ".flatpakref written"

echo "APT"
mkdeb() {  # name version arch
  local d=$T/deb/$1-$2-$3; mkdir -p "$d/DEBIAN" "$d/usr/share/doc/$1"
  printf 'Package: %s\nVersion: %s\nArchitecture: %s\nMaintainer: t <t@example.invalid>\nDescription: test\n' "$1" "$2" "$3" > "$d/DEBIAN/control"
  echo "$2" > "$d/usr/share/doc/$1/version"
  dpkg-deb --build --root-owner-group "$d" "$T/deb/$1_$2_$3.deb" >/dev/null
}
for v in 1.0 1.1 1.2; do mkdeb hello-test "$v" amd64; done
mkdeb hello-test-data 1.2 all
ice publish-deb --app hello --commit abc1234 "$T"/deb/hello-test_1.0_amd64.deb 2>/dev/null
ice publish-deb --app hello --commit abc1234 "$T"/deb/hello-test_1.1_amd64.deb 2>/dev/null
ice publish-deb --app hello --commit def5678 "$T"/deb/hello-test_1.2_amd64.deb "$T"/deb/hello-test-data_1.2_all.deb 2>/dev/null
ls "$SITE"/apt/pool/main/hello/ | tr '\n' ' ' | grep -q 'hello-test_1.1_amd64.deb hello-test_1.2_amd64.deb' \
  && ! ls "$SITE"/apt/pool/main/hello/hello-test_1.0_amd64.deb 2>/dev/null && pass "keeps the newest 2 versions"
P=$SITE/apt/dists/test/main/binary-arm64/Packages
grep -q '^Package: hello-test-data$' "$P" && ! grep -q '^Package: hello-test$' "$P" && pass "arch:all in arm64, amd64-only package not"
gpgv --keyring "$T/pub.gpg" "$SITE/apt/dists/test/InRelease" 2>/dev/null && pass "InRelease verifies"
gpgv --keyring "$T/pub.gpg" "$SITE/apt/dists/test/Release.gpg" "$SITE/apt/dists/test/Release" 2>/dev/null && pass "Release.gpg verifies"
# a real apt client against the repository, isolated from the system
A=$T/aptroot; mkdir -p "$A"/etc/apt/{sources.list.d,preferences.d,apt.conf.d} "$A"/var/lib/apt/lists/partial "$A"/var/cache/apt/archives/partial "$A"/var/lib/dpkg
touch "$A/var/lib/dpkg/status"
echo "deb [signed-by=$T/pub.gpg] file://$SITE/apt test main" > "$A/etc/apt/sources.list"
APT=(-o Dir="$A" -o Dir::State::status="$A/var/lib/dpkg/status" -o APT::Architecture=amd64 -o Debug::NoLocking=1)
apt-get "${APT[@]}" update -qq 2>&1 | grep -iE 'err|warn' && fail "apt-get update"
[ "$(apt-cache "${APT[@]}" policy hello-test | awk '/Candidate/{print $2}')" = 1.2 ] && pass "apt-get update + candidate 1.2"

echo "Android"
if command -v aapt >/dev/null && command -v apksigner >/dev/null && ls /usr/lib/android-sdk/platforms/*/android.jar >/dev/null 2>&1; then
  JAR=$(ls /usr/lib/android-sdk/platforms/*/android.jar | head -n1)
  keytool -genkeypair -keystore "$T/apk.p12" -storetype PKCS12 -alias a -keyalg RSA -keysize 2048 -validity 100 \
    -dname CN=app -storepass apkpass >/dev/null 2>&1
  mkapk() {  # versionCode versionName
    local d=$T/apk/$1; mkdir -p "$d"
    cat > "$d/AndroidManifest.xml" <<EOF
<manifest xmlns:android="http://schemas.android.com/apk/res/android" package="io.example.hello"
  android:versionCode="$1" android:versionName="$2"><uses-sdk android:minSdkVersion="21" android:targetSdkVersion="23"/>
  <application android:label="Hello"/></manifest>
EOF
    aapt package -f -M "$d/AndroidManifest.xml" -I "$JAR" -F "$d/u.apk" >/dev/null
    zipalign -f 4 "$d/u.apk" "$d/a.apk"
    apksigner sign --ks "$T/apk.p12" --ks-pass pass:apkpass --out "$T/hello-$2.apk" "$d/a.apk"
  }
  for i in 1 2 3; do mkapk "$i" "1.$i"; done
  ice fdroid-keygen --dir "$T/fdkey" 2>/dev/null
  . "$T/fdkey/env"
  for i in 1 2 3; do ice publish-apk --app hello --commit abc1234 "$T/hello-1.$i.apk" 2>"$T/log-apk$i" || { cat "$T/log-apk$i"; fail publish-apk; }; done
  [ -f "$SITE/fdroid/repo/index-v1.jar" ] && [ -f "$SITE/fdroid/repo/entry.jar" ] && pass "signed F-Droid index"
  [ "$(ls "$SITE"/fdroid/repo/*.apk | wc -l)" = 2 ] && pass "keeps the newest 2 APKs"
  python3 -c "import json,sys; b=json.load(open('$SITE/meta/builds.json'))['hello']['android']; assert b['version']=='1.3' and b['version_code']==3, b" \
    && pass "page shows versionName 1.3"
  grep -q 'fingerprint=[0-9A-F]\{64\}' "$SITE/index.html" && pass "repo link carries the fingerprint"
else
  echo "  --  skipped: aapt, apksigner or android.jar missing"
fi

echo "Windows"
head -c 4096 /dev/urandom > "$T/Hello-Setup.exe"
ice publish-windows --app hello --version "1.0.0+abc1234" --commit abc1234 --dry-run "$T/Hello-Setup.exe" 2>/dev/null
python3 -c "import json; f=json.load(open('$SITE/windows/hello.json'))['files'][0]; assert f['url'].endswith('/hello-v1.0.0-abc1234/Hello-Setup.exe'), f" \
  && pass "feed points at the release asset"

echo "Page"
H=$SITE/index.html
for s in 'id="hello"' 'Flatpak (Linux)' 'Debian / Ubuntu (.deb)' 'Windows <span' 'id="setup-flatpak"' 'id="setup-apt"' 'A <b>note</b>'; do
  grep -qF "$s" "$H" || fail "index.html lacks $s"
done
grep -qF 'id="setup-android"' "$H" || ! command -v aapt >/dev/null || fail "index.html lacks Android setup"
python3 -c "import html.parser,sys; html.parser.HTMLParser().feed(open('$H').read())" && pass "index.html has every section"

echo "Push"
ice push 2>"$T/log" || { cat "$T/log"; fail push; }
git clone -q --branch gh-pages "$T/remote.git" "$T/check"
[ "$(git -C "$T/check" rev-list --count HEAD)" = 1 ] && pass "Pages branch is one parentless commit"
[ -f "$T/check/flatpak/repo/summary" ] && [ ! -e "$T/check/flatpak/repo/tmp" ] && pass "repo pushed, tmp/ ignored"
ice publish-windows --app hello --version 1.0.1 --dry-run "$T/Hello-Setup.exe" 2>/dev/null
ice push 2>/dev/null
git -C "$T/check" fetch -q origin && [ "$(git -C "$T/check" rev-list --count origin/gh-pages)" = 1 ] && pass "second push still one commit"
# someone else publishes in between: the lease must refuse
(cd "$T/check" && git checkout -q -B gh-pages origin/gh-pages && echo x > x && git add x && git commit -qm other && git push -q origin gh-pages)
ice publish-windows --app hello --version 1.0.2 --dry-run "$T/Hello-Setup.exe" 2>/dev/null
if ice push 2>/dev/null; then fail "push overwrote someone else's publish"; else pass "stale push refused"; fi
ice sync --force 2>/dev/null && [ -f "$SITE/x" ] && pass "sync --force picks up the other publish"
ice check >/dev/null 2>&1 && pass "check"
echo "all passed"
