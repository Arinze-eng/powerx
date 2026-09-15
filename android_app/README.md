# PowerX Android Client (Native)

A **fully native Flutter client** for your self-hosted [PowerX](https://http--powerx--mxq9vl6k966n.code.run/) AI agent (nanobot gateway). No WebView — it speaks directly to the backend's auth + chat APIs and renders a custom native UI.

## Architecture

```
lib/
├── config.dart                  # Backend URL + WS origin derivation
├── models.dart                  # ChatMessage, SessionSummary, ThreadTurn parsing
├── state/app_state.dart         # Auth + bootstrap + sessions + socket orchestration
├── services/
│   ├── supabase_auth.dart       # Email/password login & signup via Supabase REST
│   ├── gateway_api.dart         # /webui/bootstrap, /api/sessions, thread history
│   └── nanobot_socket.dart      # WebSocket chat protocol (new_chat / message / delta)
└── screens/
    ├── auth_screen.dart         # Sign in / sign up
    ├── home_screen.dart         # Sessions drawer + landing
    └── chat_screen.dart         # Native bubbles, streaming, markdown composer
```

### How it wires to your service

1. **Discover** — `GET /webui/bootstrap` returns the Supabase URL + anon key (nothing baked into the app).
2. **Auth** — email/password against Supabase (`/auth/v1/token?grant_type=password`). Signup requires email confirmation.
3. **Exchange** — `GET /webui/bootstrap` with header `X-Nanobot-Auth: <supabase_token>` → gateway WS token + REST api_token + ws_path.
4. **Sessions** — `GET /api/sessions` (Bearer) lists chats; `GET /api/sessions/{key}/webui-thread` loads history.
5. **Chat** — WebSocket to `wss://host{ws_path}?token=…`; send `{"type":"new_chat"}` then `{"type":"message","chat_id":…,"content":…,"webui":true}`; stream `delta` → `stream_end` (or final `message`) frames into the UI live.

Tokens persist securely (`flutter_secure_storage`) so sign-in survives restarts; expired tokens auto-refresh once.

## Configuration

Default backend:

```
https://http--powerx--mxq9vl6k966n.code.run
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
flutter test
flutter build apk --release   # build/app/outputs/flutter-apk/app-release.apk
```

## Signing

Debug-signed release APK installs immediately for personal use. For Play Store
distribution, add a real keystore and update `android/app/build.gradle.kts`.
