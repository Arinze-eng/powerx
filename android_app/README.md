# PowerX Android Client

A **native-feeling** Flutter client for your self-hosted [PowerX](https://http--powerx--mxq9vl6k966n.code.run/) AI agent (nanobot gateway).

## Design: it doesn't look like a web app

The client renders the hosted PowerX WebUI but strips every browser tell so it
reads as a first-class native app:

- **No browser chrome** — no URL bar, no reload button, no title bar. Edge-to-edge immersive layout.
- **Branded splash** — a polished PowerX loading screen covers the cold start; no white flash (dark window background matches the app).
- **No web gestures** — pinch-zoom, double-tap-zoom, pull-to-refresh bounce, long-press context menu, text-selection handles, and drag navigation are all disabled via injected CSS/JS.
- **Hidden scrollbars**, tap-highlight suppressed, overscroll removed.
- Chat inputs and message text remain selectable so copying still works.
- Native back gesture walks web history, then exits cleanly.
- File downloads from the agent save to device storage with an "Open" snackbar; external links open in the system browser.

All authentication (Supabase) happens inside the hosted WebUI itself — the shell
just presents it natively.

## Configuration

Default backend:

```
https://http--powerx--mxq9vl6k966n.code.run/
```

Override at build time:

```bash
flutter build apk --release --dart-define=POWERX_URL=https://your-host
```

## Building the APK (GitHub Actions only)

APK builds run entirely on **GitHub Actions** — no local Android toolchain needed.

1. Push changes under `android_app/` to `main`, **or**
2. **Actions → Build PowerX Android APK → Run workflow** (optionally pass a custom `powerx_url`).

Download the artifact **`powerx-android-release-apk`** (`app-release.apk`) from the workflow summary and install it on your device.

### Local build (optional, development only)

```bash
cd android_app
flutter pub get
flutter build apk --release   # build/app/outputs/flutter-apk/app-release.apk
```

## Signing

Debug-signed release APK installs immediately for personal use. For Play Store
distribution, add a real keystore and update `android/app/build.gradle.kts`.
