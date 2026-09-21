# PowerX Android Client (Native)

A **fully native Flutter client** for your self-hosted [PowerX](https://http--powerx--mxq9vl6k966n.code.run/) AI agent (nanobot gateway). No WebView — it speaks directly to the backend's auth + chat APIs and renders a custom native UI.

## Architecture

```
lib/
├── config.dart                  # Backend URL + WS origin derivation
├── models.dart                  # ChatMessage, SessionSummary, ThreadTurn parsing
├── state/
│   ├── app_state.dart           # Auth + bootstrap + sessions + socket orchestration
│   └── theme_controller.dart    # Light/dark/system, persisted choice
├── theme/
│   ├── tokens.dart              # WebPalette: every webui design token
│   ├── app_theme.dart           # ThemeData for light + dark
│   └── palette.dart             # context.palette facade over the active token set
├── services/
│   ├── supabase_auth.dart       # Email/password login & signup via Supabase REST
│   ├── gateway_api.dart         # /webui/bootstrap, /api/sessions, thread history
│   ├── settings_api.dart        # /api/settings, /workspaces, /version, mutations
│   └── nanobot_socket.dart      # WebSocket chat protocol (new_chat / attach / message / delta)
├── widgets/
│   ├── brand.dart               # BrandMark (webui orange robot), wordmark, avatars
│   ├── sidebar.dart             # Drawer: brand, actions, search, grouped sessions
│   ├── sidebar_actions.dart     # New chat / Search / Apps / Skills / Automations / Archive
│   ├── settings_controls.dart   # Web-styled rows, groups, toggles, segmented control
│   ├── connection_badge.dart    # Socket health dot
│   └── theme_toggle.dart        # Sun/moon switch, same behaviour as the web
└── screens/
    ├── auth_screen.dart         # Sign in / sign up
    ├── home_screen.dart         # Landing + sessions drawer
    ├── chat_screen.dart         # Native bubbles, streaming, markdown composer
    ├── settings_screen.dart     # Native settings mirroring the web sections
    └── settings_sections.dart   # Section ids/order shared with the sidebar
```

## Design system (parity with `webui`)

The APK renders the same design language as the web UI. Nothing is hand-picked:
every colour, radius, type size and spacing value is transcribed from
`webui/src/globals.css` into `lib/theme/tokens.dart`, and
`test/design_system_test.dart` asserts the hex values against the CSS so the two
clients cannot drift silently.

- **`WebPalette`** — the full token set per scheme (`WebPalette.light` /
  `WebPalette.dark`), exposed as a `ThemeExtension`. This is the single source of
  truth; new code reads `context.palette` (or `WebPalette.of(context)`) and never
  hard-codes a colour. `lib/theme/palette.dart` is a thin back-compat facade over
  the same tokens for older call sites.
- **Themes** — `AppTheme.light()` / `AppTheme.dark()` / `AppTheme.forMode()`
  build `ThemeData` from those tokens (canvas app bar, 12px muted titles,
  primary-filled buttons, `#2997FF` switches), plus the per-scheme markdown
  stylesheet.
- **Mode** — `ThemeController` defaults to `ThemeMode.system`, follows the
  phone's light/dark setting live (`didChangePlatformBrightness`), and persists a
  pinned choice in secure storage under `theme_mode`. `ThemeToggleButton` is the
  sun/moon control the web header has; it sits on the workspace bar and in the
  chat app bar. The Android launch themes (`values/`, `values-night/`) use the
  same canvas so the splash never flashes the wrong colour.
- **Brand** — the CDNAI mark is the web's own orange robot
  (`webui/public/brand/nanobot_mark.svg`), rasterised into `assets/brand/` and
  every launcher density by `tool/generate_brand_assets.sh`.

### How it wires to your service

1. **Discover** — `GET /webui/bootstrap` returns the Supabase URL + anon key (nothing baked into the app).
2. **Auth** — email/password against Supabase (`/auth/v1/token?grant_type=password`). Signup requires email confirmation.
3. **Exchange** — `GET /webui/bootstrap` with header `X-Nanobot-Auth: <supabase_token>` → gateway WS token + REST api_token + ws_path.
4. **Sessions** — `GET /api/sessions` (Bearer) lists chats; `GET /api/sessions/{key}/webui-thread` loads history.
5. **Chat** — WebSocket to `wss://host{ws_path}?token=…`; send `{"type":"new_chat"}` then `{"type":"message","chat_id":…,"content":…,"webui":true}`; stream `delta` → `stream_end` (or final `message`) frames into the UI live.

   The WS handshake takes the **gateway `token`** from the bootstrap payload, not
   the REST `api_token`; sending the REST token is rejected with `401`.

### Cross-client session continuity

A task started in the app must appear, and keep streaming, in the web UI on any
other device — and vice versa. Two protocol facts make that work, both covered by
`test/socket_protocol_test.dart`:

- **A socket only receives a chat's turn frames while it is *attached* to that
  chat.** Creating a chat is not subscribing to it, and the subscription is
  dropped with the connection. `sendMessage()` therefore emits an `attach` before
  the `message` whenever the chat is not yet confirmed on this connection, and
  `connect()` re-subscribes every wanted chat on a fresh socket — before it
  flushes frames queued while offline, so a buffered task can never reach the
  gateway ahead of its own subscription.
- **Turns are owned by the server and are shared by chat**, so the second client
  simply attaches (or reattaches on reopen) and receives the running turn's
  replay. Nothing is stored client-side that would make a chat exclusive to one
  device.

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
