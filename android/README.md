# Valley Lotto — Android app

A thin shell around the web app, for the tablet or phone at the counter.

**Why an app rather than the browser:** a home-screen icon, no browser chrome to
mis-tap, a screen that won't sleep in the middle of a count, and a login session
that survives for weeks.

**Scanning needs no native code.** A retail scan gun types the barcode like a
keyboard, so the page's own scan field receives it exactly as it does on a
desktop — the app doesn't intercept anything.

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

The build is signed with the standard Android debug key, so it installs without
a Play Store account. It is not for public distribution — it's an internal tool
for your own stores.

## Building locally (optional)

Needs Android Studio or the command-line SDK:

```bash
cd android
./gradlew assembleDebug
# app/build/outputs/apk/debug/app-debug.apk
```

## Layout

| File | Purpose |
|------|---------|
| `app/src/main/java/.../MainActivity.kt` | The WebView shell: server prompt, cookie persistence, keep-awake, offline notice |
| `app/src/main/AndroidManifest.xml` | Permissions (internet only) and the launcher activity |
| `app/src/main/res/` | Brand colors, strings, and the logo launcher icon |
| `../.github/workflows/android.yml` | The CI build that produces the APK |
