# PowerX Android Client

A lightweight **Flutter WebView wrapper** for your self-hosted [PowerX](https://http--powerx--mxq9vl6k966n.code.run/) AI agent (nanobot gateway).

## What it does

- Opens the hosted PowerX WebUI inside a native Android WebView.
- All authentication (Supabase) and chat happen **inside the web app itself** — the shell just renders it reliably.
- Handles file downloads from the agent into device storage.
- External links open in the system browser; back button navigates web history.
- **No "Tools" section** — this is a single-screen client wired directly to your service.

## Configuration

The backend URL defaults to:

```
https://http--powerx--mxq9vl6k966n.code.run/
```

Override at build time with:

```bash
flutter build apk --release --dart-define=POWERX_URL=https://your-host
```

## Building the APK (GitHub Actions)

APK builds run entirely on **GitHub Actions** — no local Android toolchain required.

1. Push changes under `android_app/` to `main`, **or**
2. Go to **Actions → Build PowerX Android APK → Run workflow** (manual trigger), optionally passing a custom `powerx_url`.

When the run finishes, download the artifact **`powerx-android-release-apk`** (`app-release.apk`) from the workflow summary page and install it on your device.

### Local build (optional, for development)

Requires Flutter 3.29+ and the Android SDK:

```bash
cd android_app
flutter pub get
flutter build apk --release
# Output: build/app/outputs/flutter-apk/app-release.apk
```

## Signing

The release build uses the debug signing config so the APK installs immediately
for personal use. For Play Store distribution, add a real keystore and update
`android/app/build.gradle.kts`.
