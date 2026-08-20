# Valley Lotto — Android app

A thin shell around the web app, for the tablet or phone at the counter.

**Why an app rather than the browser:** a home-screen icon, no browser chrome to
mis-tap, a screen that won't sleep in the middle of a count, and a login session
that survives for weeks.

**Scanning needs no native code.** A retail scan gun types the barcode like a
keyboard, so the page's own scan field receives it exactly as it does on a
desktop — the app doesn't intercept the scan itself.

**The keyboard is the app's job, though.** A web page can *ask* Android not to
raise the on-screen keyboard; it cannot insist, and on many phones it is
overruled — which is why the keyboard kept appearing on the scan screen in a
browser. The app holds it down from the outside: the window opens with the
keyboard suppressed, and any attempt to raise it is pushed straight back down.
Tapping the ⌨ button on the scan screen is the one thing that lifts that, and
leaving the field puts it back.

If you are using the site in a plain browser rather than the app, the scan
screen has three fallbacks under "Scanner not picking up?" — see that menu. It
is also worth turning off Android's own setting, which exists exactly for this:

> Settings → System → Languages & input → **Physical keyboard** →
> *Show on-screen keyboard* → **off**

With a scan gun paired, that stops the on-screen keyboard appearing for any app
on the device.

## Getting the APK

The APK is built by GitHub Actions (their runners already have the Android SDK):

1. Go to the repo's **Actions** tab → **Build Android APK**.
2. Open the newest run → **Artifacts** → download `valley-lotto-apk`.
3. Unzip and copy the `.apk` to the device.

To build on demand, use **Run workflow** on that page. Tagging a release
(`git tag v1.0 && git push --tags`) also attaches the APK to a GitHub Release,
which gives you a plain download link to open on the device itself.

## Installing on the device

1. Settings → **Allow installs from unknown sources** for your file manager or browser.
2. Tap the `.apk` → Install.
3. On first launch it asks for the **store's website address** (the Railway URL).
   Enter it once. Long-press the screen later to change it.

The app is not for public distribution — it's an internal tool for your own
stores, so it installs from the file rather than from the Play Store.

### "App not installed as package conflicts with an existing package"

Android only replaces an app when the new APK carries the **same signing key**
as the installed one. Builds before this was set up were signed with the CI
runner's automatic debug key, which is regenerated on every run — so each APK
had a different key and the phone refused the next one.

If you see this message, uninstall the app once (Settings → Apps → Valley Lotto
→ Uninstall) and install again. You only lose the saved server address and the
sign-in cookie; nothing counted is on the phone. From then on updates install
over the top.

## Signing (one-time setup)

The key lives in repository secrets, never in the repo:

| Secret | What it is |
|--------|------------|
| `ANDROID_KEYSTORE_BASE64` | the `.jks` keystore, base64-encoded |
| `ANDROID_KEYSTORE_PASSWORD` | its password |

Add them under **Settings → Secrets and variables → Actions → New repository
secret**. With both present, every build is signed with the same key and its
version code comes from the workflow run number, so each APK outranks the last.

Without them the build still succeeds, but the run logs a warning and the APK
won't install over an existing copy.

Keep a backup of the keystore file somewhere safe. Losing it means every device
has to uninstall and reinstall once more.

To create a replacement:

```bash
keytool -genkeypair -v -keystore valley-lotto.jks -alias valleylotto \
  -keyalg RSA -keysize 4096 -validity 10950
base64 -w0 valley-lotto.jks      # paste into ANDROID_KEYSTORE_BASE64
```

## Building locally (optional)

Needs Android Studio or the command-line SDK:

```bash
cd android
./gradlew assembleDebug
# app/build/outputs/apk/debug/app-debug.apk
```

A local build uses your own debug key, so it won't install over a CI-signed
copy (and vice versa) — that's for trying changes out, not for a store device.

## Layout

| File | Purpose |
|------|---------|
| `app/src/main/java/.../MainActivity.kt` | The WebView shell: server prompt, cookie persistence, keep-awake, offline notice |
| `app/src/main/AndroidManifest.xml` | Permissions (internet only) and the launcher activity |
| `app/src/main/res/` | Brand colors, strings, and the logo launcher icon |
| `../.github/workflows/android.yml` | The CI build that produces the APK |
