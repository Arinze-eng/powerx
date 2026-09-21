import 'dart:async';
import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';

import '../models.dart';
import '../services/chat_cache.dart';
import '../services/device_id.dart';
import '../services/gateway_api.dart';
import '../services/nanobot_socket.dart';
import '../services/pending_sends.dart';
import '../services/settings_api.dart';
import '../services/supabase_auth.dart';

enum AppStatus { loading, unauthenticated, authenticating, authenticated, error }

/// Mirrors `webui/src/lib/types.ts` `ConnectionStatus`, so the badge reads the
/// same on both clients.
enum AppSocketStatus { idle, connecting, open, reconnecting, closed, error }

/// Gateway token TTL is short (300 s observed). Refresh well before expiry,
/// mirroring the WebUI constants (margin 30 s, minimum delay 5 s).
const _kTokenRefreshMargin = Duration(seconds: 30);
const _kTokenRefreshMinDelay = Duration(seconds: 5);

Duration tokenRefreshDelay(Duration remaining) {
  final margin = remaining ~/ 2 < _kTokenRefreshMargin
      ? (remaining ~/ 2 < const Duration(seconds: 1)
          ? const Duration(seconds: 1)
          : remaining ~/ 2)
      : _kTokenRefreshMargin;
  final d = remaining - margin;
  return d < _kTokenRefreshMinDelay ? _kTokenRefreshMinDelay : d;
}

/// Central app state: owns the Supabase session, gateway tokens (auto-refresh
/// because they expire in minutes), the shared chat socket, and sessions.
class AppState extends ChangeNotifier {
  final GatewayApi api = GatewayApi();
  final FlutterSecureStorage _storage = const FlutterSecureStorage();

  static const _kSbUrl = 'sb_url';
  static const _kSbKey = 'sb_key';
  static const _kAccess = 'access_token';
  static const _kRefresh = 'refresh_token';
  static const _kEmail = 'email';
  static const _kName = 'name';
  static const _kLastChat = 'last_chat_id';
  static const _kSessionCache = 'sessions_cache';
  static const _kOpenChat = 'open_chat_v1';

  /// Local transcript persistence so a chat renders instantly and survives a
  /// backgrounded turn that the gateway does not replay over the socket.
  final ChatCache chatCache = ChatCache();

  /// Durable queue of user messages that have not been confirmed by the
  /// gateway yet, so a task survives the app being killed mid-send.
  final PendingSendQueue pendingSends =
      PendingSendQueue(SecureKeyValueStore());

  AppStatus status = AppStatus.loading;
  String? errorMessage;

  SupabaseAuth? _auth;
  String? accessToken;
  String? refreshToken;
  String? email;
  String? displayName;

  // Gateway bootstrap material
  String? _apiToken;

  /// Gateway REST token (used for authenticated file downloads from chat).
  String? get apiToken => _apiToken;
  String? _wsToken;
  String? _wsPath;
  DateTime? _tokenExpiresAt;
  DateTime? _sbExpiresAt;
  Timer? _tokenTimer;
  NanobotSocket? _socket;

  // Bootstrap-derived profile/billing data
  String? modelName;
  String? supabaseUserId;
  List<PaymentPackage> paymentPackages = const [];
  String paymentUrl = '';

  CreditBundle? credits;
  bool creditsLoading = false;

  List<SessionSummary> sessions = [];
  bool sessionsLoading = false;

  String? lastChatId;

  /// Whether the chat socket is currently connected.
  bool socketConnected = false;

  /// Socket state for the connection badge, mirroring the web's
  /// `ConnectionStatus` union so the indicator reads the same on both clients.
  AppSocketStatus socketStatus = AppSocketStatus.idle;

  // ---- Settings ---------------------------------------------------------
  /// REST client for the gateway's settings surface. Reads go over HTTP,
  /// writes go over the chat socket (see [mutate]).
  final SettingsApi settingsApi = SettingsApi();

  SettingsSnapshot? settings;
  bool settingsLoading = false;
  String? settingsError;
  List<NanobotFeature> features = const [];
  List<SkillInfo> skills = const [];
  List<McpPreset> mcpPresets = const [];
  List<PairingRequestInfo> pairing = const [];
  List<CliAppInfo> cliApps = const [];
  List<AutomationJob> automations = const [];
  AppVersionInfo? appVersion;

  /// Load the whole settings surface in one round trip. Individual failures are
  /// tolerated: the account that has not enabled image generation still gets a
  /// usable Models/Appearance page rather than an error screen.
  Future<void> loadSettings() async {
    final api = _apiToken;
    if (api == null) return;
    settingsLoading = true;
    settingsError = null;
    notifyListeners();
    final sb = accessToken;
    try {
      settings = await settingsApi.fetchSettings(api, sb);
    } catch (e) {
      settingsError = e.toString();
    }
    Future<void> soft<T>(Future<T> Function() run, void Function(T) keep) async {
      try {
        keep(await run());
      } catch (_) {
        // Optional section: leave the previous value in place.
      }
    }

    await Future.wait([
      soft(() => settingsApi.fetchFeatures(api, sb), (v) => features = v),
      soft(() => settingsApi.fetchSkills(api, sb), (v) => skills = v),
      soft(() => settingsApi.fetchMcpPresets(api, sb), (v) => mcpPresets = v),
      soft(() => settingsApi.fetchPairing(api, sb), (v) => pairing = v),
      soft(() => settingsApi.fetchVersion(api, sb), (v) => appVersion = v),
    ]);
    settingsLoading = false;
    notifyListeners();
  }

  /// Refresh just the settings document after a successful write.
  Future<void> refreshSettings() async {
    final api = _apiToken;
    if (api == null) return;
    try {
      settings = await settingsApi.fetchSettings(api, accessToken);
      notifyListeners();
    } catch (_) {}
  }

  /// Run one WebUI mutation over the authenticated socket.
  ///
  /// This is the *only* channel the gateway accepts settings writes on: a plain
  /// HTTP POST to `/api/settings/**` is rejected with
  /// `405 WebUI mutations require an authenticated WebSocket`. Routing every
  /// write through here is what keeps the app at parity with the browser.
  Future<Map<String, dynamic>> mutate(
    String action,
    Map<String, dynamic> payload,
  ) async {
    final sock = await ensureSocket();
    return sock.mutate(action, payload);
  }

  /// Discover Supabase config from the gateway and restore any saved session.
  Future<void> init() async {
    try {
      final boot = await api.bootstrap();
      if (boot.supabaseUrl != null && boot.supabaseAnonKey != null) {
        await _storage.write(key: _kSbUrl, value: boot.supabaseUrl!);
        await _storage.write(key: _kSbKey, value: boot.supabaseAnonKey!);
        _auth = SupabaseAuth(url: boot.supabaseUrl!, anonKey: boot.supabaseAnonKey!);
      }
      await _restore();
    } catch (e) {
      _fail('Cannot reach CDNAI service: $e');
    }
  }

  Future<void> _restore() async {
    // Never let a stale restore mix with a fresh sign-in on the same device.
    _resetIdentity(wipeStorage: false);
    lastChatId = await _storage.read(key: _kLastChat);
    final url = await _storage.read(key: _kSbUrl);
    final key = await _storage.read(key: _kSbKey);
    final at = await _storage.read(key: _kAccess);
    final rt = await _storage.read(key: _kRefresh);
    if (url != null && key != null) {
      _auth = SupabaseAuth(url: url, anonKey: key);
    }
    if (at != null && rt != null && _auth != null) {
      accessToken = at;
      refreshToken = rt;
      email = await _storage.read(key: _kEmail);
      displayName = await _storage.read(key: _kName);
      try {
        await _bootstrapGateway();
        status = AppStatus.authenticated;
        notifyListeners();
        unawaited(loadSessions());
        unawaited(refreshCredits());
        // Cold start after an app kill: reconnect and re-subscribe the chat
        // that was open so an in-flight task keeps streaming (steps + answer)
        // instead of the user finding a frozen transcript.
        unawaited(resumeOpenChat());
      } catch (_) {
        try {
          await _refreshSupabase();
          await _bootstrapGateway();
          status = AppStatus.authenticated;
          notifyListeners();
          unawaited(loadSessions());
          unawaited(refreshCredits());
          unawaited(resumeOpenChat());
        } catch (e) {
          _resetIdentity(wipeStorage: false);
          await _clearSession();
          status = AppStatus.unauthenticated;
          notifyListeners();
        }
      }
    } else {
      status = AppStatus.unauthenticated;
      notifyListeners();
    }
  }

  Future<void> signIn(String em, String pw) async {
    if (_auth == null) {
      _fail('Service not initialized');
      return;
    }
    status = AppStatus.authenticating;
    errorMessage = null;
    // Hard identity boundary: nothing from a previous account may survive
    // into this session, even if the new account's payload lacks fields.
    _resetIdentity();
    notifyListeners();
    try {
      final s = await _auth!.signIn(em.trim(), pw);
      await _persist(s, overwriteIdentity: true);
      await _bootstrapGateway();
      status = AppStatus.authenticated;
      notifyListeners();
      unawaited(loadSessions());
      unawaited(refreshCredits());
    } on AuthException catch (e) {
      _fail(e.message);
    } catch (e) {
      _fail('$e');
    }
  }

  /// Returns true when signed in immediately; false when email confirmation is required.
  Future<bool> signUp(String em, String pw, String name, {String? referral}) async {
    if (_auth == null) {
      _fail('Service not initialized');
      return false;
    }
    // Device lock (anti-abuse): one signup per device. Local mirror blocks
    // instantly even offline; the signup-gate edge function enforces the
    // same rule server-side against persistent hashed device records.
    try {
      if (await DeviceId.instance.signedUpLocally()) {
        final bound = await DeviceId.instance.boundEmail();
        if (bound == null || bound != em.trim().toLowerCase()) {
          _fail('An account has already been created on this device. '
              'Please sign in to your existing account instead.');
          return false;
        }
      }
    } catch (_) {/* local mirror is best-effort */}
    status = AppStatus.authenticating;
    errorMessage = null;
    _resetIdentity();
    notifyListeners();
    try {
      final fp = await DeviceId.instance.fingerprint();
      final s = await _auth!
          .signUp(em.trim(), pw, name, referral: referral, fingerprint: fp);
      if (s == null) {
        // Account created but email confirmation is pending — the device is
        // now bound either way.
        unawaited(DeviceId.instance.markSignedUp(em));
        status = AppStatus.unauthenticated;
        errorMessage = 'Check your email to confirm your account, then sign in.';
        notifyListeners();
        return false;
      }
      await _persist(s, overwriteIdentity: true);
      unawaited(DeviceId.instance.markSignedUp(em));
      final ref = (referral ?? '').trim();
      if (ref.isNotEmpty) {
        unawaited(_claimReferral(s.accessToken, ref));
      }
      await _bootstrapGateway();
      status = AppStatus.authenticated;
      notifyListeners();
      unawaited(loadSessions());
      unawaited(refreshCredits());
      return true;
    } on AuthException catch (e) {
      _fail(e.message);
      return false;
    } catch (e) {
      _fail('$e');
      return false;
    }
  }

  Future<void> _claimReferral(String token, String referral) async {
    try {
      await _auth?.claimReferral(token, referral);
    } catch (_) {/* best-effort */}
  }

  Future<void> signOut() async {
    _socket?.close();
    _socket = null;
    _tokenTimer?.cancel();
    _tokenTimer = null;
    _resetIdentity(wipeStorage: false);
    await _clearSession();
    await chatCache.clearAll();
    await pendingSends.clear();
    await _storage.delete(key: _kOpenChat);
    status = AppStatus.unauthenticated;
    notifyListeners();
  }

  /// Wipe every account-bound field. Called before applying a NEW session
  /// (sign-in/sign-up), on sign-out, and on failed restore, so switching
  /// accounts on one device can never show the previous user's name,
  /// credits, sessions or billing data.
  void _resetIdentity({bool wipeStorage = true}) {
    email = null;
    displayName = null;
    sessions = [];
    credits = null;
    creditsLoading = false;
    modelName = null;
    supabaseUserId = null;
    paymentPackages = const [];
    paymentUrl = '';
    lastChatId = null;
    _apiToken = null;
    _wsToken = null;
    _wsPath = null;
    _tokenExpiresAt = null;
    _sbExpiresAt = null;
    if (wipeStorage) {
      unawaited(_storage.delete(key: _kEmail));
      unawaited(_storage.delete(key: _kName));
      unawaited(_storage.delete(key: _kLastChat));
      unawaited(_storage.delete(key: _kSessionCache));
      unawaited(_storage.delete(key: _kOpenChat));
      unawaited(pendingSends.clear());
      unawaited(chatCache.clearAll());
    }
  }

  Future<void> _persist(SupabaseSession s,
      {bool overwriteIdentity = false}) async {
    accessToken = s.accessToken;
    refreshToken = s.refreshToken;
    if (overwriteIdentity) {
      // A brand-new sign-in defines the identity — never inherit the old one.
      email = s.email;
      displayName = s.name;
    } else {
      email = s.email ?? email;
      displayName = s.name ?? displayName;
    }
    if (s.expiresAt != null) {
      _sbExpiresAt =
          DateTime.fromMillisecondsSinceEpoch(s.expiresAt! * 1000);
    } else if (s.expiresIn != null) {
      _sbExpiresAt =
          DateTime.now().add(Duration(seconds: s.expiresIn!));
    }
    await _storage.write(key: _kAccess, value: s.accessToken);
    await _storage.write(key: _kRefresh, value: s.refreshToken);
    if (email != null) await _storage.write(key: _kEmail, value: email!);
    if (displayName != null) {
      await _storage.write(key: _kName, value: displayName!);
    }
  }

  /// Refresh the Supabase access token when it is missing or within 5 minutes
  /// of expiry (it lives ~1 h, the gateway tokens ~5 min).
  Future<void> _ensureSupabaseFresh() async {
    final exp = _sbExpiresAt;
    final needs = exp == null || exp.difference(DateTime.now()) < const Duration(minutes: 5);
    if (!needs) return;
    await _refreshSupabase();
  }

  Future<void> _clearSession() async {
    accessToken = null;
    refreshToken = null;
    await _storage.delete(key: _kAccess);
    await _storage.delete(key: _kRefresh);
    await _storage.delete(key: _kEmail);
    await _storage.delete(key: _kName);
    await _storage.delete(key: _kLastChat);
  }

  Future<void>? _refreshInFlight;
  Future<void> _refreshSupabase() {
    final inFlight = _refreshInFlight;
    if (inFlight != null) return inFlight;
    final f = _doRefreshSupabase().whenComplete(() => _refreshInFlight = null);
    _refreshInFlight = f;
    return f;
  }

  Future<void> _doRefreshSupabase() async {
    if (_auth == null || refreshToken == null) return;
    final s = await _auth!.refresh(refreshToken!);
    await _persist(s);
  }

  /// Exchange the (fresh) Supabase access token for gateway WS/REST tokens
  /// and schedule the next refresh before the short TTL elapses. Concurrent
  /// callers share one in-flight exchange (no stale-token races).
  Future<void>? _bootInFlight;
  Future<void> _bootstrapGateway() {
    final inFlight = _bootInFlight;
    if (inFlight != null) return inFlight;
    final f = _doBootstrap().whenComplete(() => _bootInFlight = null);
    _bootInFlight = f;
    return f;
  }

  Future<void> _doBootstrap() async {
    await _ensureSupabaseFresh();
    var boot = await api.bootstrap(supabaseAccessToken: accessToken);
    if (boot.needsAuth || boot.token.isEmpty) {
      // One retry after a forced Supabase refresh (token may have rotated
      // while the app slept).
      await _refreshSupabase();
      boot = await api.bootstrap(supabaseAccessToken: accessToken);
      if (boot.needsAuth || boot.token.isEmpty) {
        throw Exception('Gateway rejected authentication');
      }
    }
    _applyBoot(boot);
  }

  void _applyBoot(GatewayBootstrap boot) {
    // Defensive: if the gateway resolved a DIFFERENT Supabase user than the
    // previous bootstrap (identity switch), purge all per-user views first.
    if (supabaseUserId != null &&
        boot.supabaseUserId != null &&
        boot.supabaseUserId != supabaseUserId) {
      sessions = [];
      credits = null;
      lastChatId = null;
    }
    _apiToken = boot.apiToken;
    _wsToken = boot.token;
    _wsPath = boot.wsPath;
    modelName = boot.modelName;
    supabaseUserId = boot.supabaseUserId;
    paymentPackages = boot.paymentPackages;
    paymentUrl = boot.paymentUrl;
    if ((boot.userEmail ?? '').isNotEmpty) email = boot.userEmail;
    // Live server value: expires_in=300. Trust the payload, default to 4 min.
    final ttl = boot.expiresInSeconds != null && boot.expiresInSeconds! > 30
        ? boot.expiresInSeconds! - 60
        : 240;
    _tokenExpiresAt = DateTime.now().add(Duration(seconds: ttl));
    _scheduleTokenRefresh();
  }

  /// Gateway tokens expire in ~5 minutes (observed expires_in=300). Keep them
  /// hot so history reads, credits, and reconnects never 401 mid-session.
  void _scheduleTokenRefresh() {
    _tokenTimer?.cancel();
    final expires = _tokenExpiresAt;
    if (expires == null || status != AppStatus.authenticated) return;
    final remaining = expires.difference(DateTime.now());
    _tokenTimer = Timer(tokenRefreshDelay(remaining < Duration.zero ? const Duration(seconds: 5) : remaining), () async {
      if (status != AppStatus.authenticated) return;
      try {
        await _refreshSupabase();
        await _bootstrapGateway();
        // The next reconnect / REST call picks up fresh tokens automatically.
        notifyListeners();
      } catch (_) {
        // Retry sooner; a persistent failure signs the user out on next REST 401.
        _tokenTimer = Timer(const Duration(seconds: 20), () {
          _bootstrapGateway().catchError((_) {});
        });
      }
    });
  }

  /// Force an immediate token refresh (used after a 401 on REST calls).
  Future<void> rebootstrap() async {
    _tokenTimer?.cancel();
    try {
      await _refreshSupabase();
    } catch (_) {}
    await _bootstrapGateway();
    notifyListeners();
  }

  String get greetingName {
    final n = (displayName ?? '').trim();
    if (n.isNotEmpty) return n.split(RegExp(r'\s+')).first;
    if (email != null && email!.contains('@')) return email!.split('@').first;
    return 'there';
  }

  // ---- Credits / billing ------------------------------------------------

  Future<void> refreshCredits() async {
    if (_auth == null || accessToken == null) return;
    creditsLoading = true;
    notifyListeners();
    try {
      credits = await _auth!.fetchCredits(accessToken!);
    } catch (_) {
      // keep previous
    } finally {
      creditsLoading = false;
      notifyListeners();
    }
  }

  Future<VerifyPaymentResult> verifyPayment(String txRef, {String? transactionId}) async {
    if (_auth == null || accessToken == null) {
      return const VerifyPaymentResult(ok: false, error: 'Please sign in again.');
    }
    final res = await _auth!.verifyPayment(accessToken!, txRef, transactionId: transactionId);
    if (res.ok) await refreshCredits();
    return res;
  }

  Future<bool?> referralUsedStatus() async {
    if (_auth == null || accessToken == null || email == null) return null;
    return _auth!.referralUsed(accessToken!, email!);
  }

  // ---- Sessions ---------------------------------------------------------

  Future<void> loadSessions() async {
    if (_apiToken == null) {
      // Before bootstrap completes, show whatever we cached last session so
      // the drawer is never empty on a cold, offline start.
      await _restoreSessionCache();
      return;
    }
    sessionsLoading = true;
    notifyListeners();
    try {
      sessions = await api.listSessions(_apiToken!, supabaseToken: accessToken);
      _storeSessionCache(sessions);
    } on ApiException catch (e) {
      if (e.status == 401) {
        await rebootstrap();
        try {
          sessions = await api.listSessions(_apiToken!, supabaseToken: accessToken);
          _storeSessionCache(sessions);
        } catch (_) {
          await _restoreSessionCache();
        }
      } else {
        await _restoreSessionCache();
      }
    } catch (_) {
      // Network blip: keep the previous list, or rehydrate from disk.
      if (sessions.isEmpty) await _restoreSessionCache();
    } finally {
      sessionsLoading = false;
      notifyListeners();
    }
  }

  Future<void> _storeSessionCache(List<SessionSummary> rows) async {
    try {
      final encoded = jsonEncode(rows
          .map((s) => {
                'key': s.key,
                'title': s.title,
                'preview': s.preview,
                if (s.updatedAt != null)
                  'updated_at': s.updatedAt!.toUtc().toIso8601String(),
              })
          .toList());
      await _storage.write(key: _kSessionCache, value: encoded);
    } catch (_) {}
  }

  Future<void> _restoreSessionCache() async {
    if (sessions.isNotEmpty) return;
    try {
      final raw = await _storage.read(key: _kSessionCache);
      if (raw == null || raw.isEmpty) return;
      final list = jsonDecode(raw);
      if (list is! List) return;
      final rows = list
          .whereType<Map>()
          .map((r) => SessionSummary.fromJson(Map<String, dynamic>.from(r)))
          .toList();
      if (rows.isEmpty) return;
      sessions = rows;
      notifyListeners();
    } catch (_) {}
  }

  /// Load one conversation. The local cache is returned immediately as
  /// [ThreadHistory.messages] is merged with the authoritative server copy, so
  /// reopening the app shows prior answers even when the turn was still
  /// streaming (the gateway does not replay delta history over the socket).
  Future<ThreadHistory> openSession(SessionSummary s) async {
    final cached = await chatCache.load(s.chatId);
    if (_apiToken == null) {
      // Not bootstrapped yet: better a cached transcript than a blank screen.
      return ThreadHistory(messages: cached);
    }
    try {
      final server = await api.fetchThread(_apiToken!, s.key,
          supabaseToken: accessToken);
      await _storeSessionCache(sessions);
      return ThreadHistory(
        messages: mergeThreadHistory(server: server.messages, cached: cached),
        activeTurnId: server.activeTurnId,
        hasPendingToolCalls: server.hasPendingToolCalls,
      );
    } on ApiException catch (e) {
      if (e.status == 401) {
        await rebootstrap();
        try {
          final server = await api.fetchThread(_apiToken!, s.key,
              supabaseToken: accessToken);
          return ThreadHistory(
            messages: mergeThreadHistory(server: server.messages, cached: cached),
            activeTurnId: server.activeTurnId,
            hasPendingToolCalls: server.hasPendingToolCalls,
          );
        } catch (_) {
          if (cached.isNotEmpty) return ThreadHistory(messages: cached);
          rethrow;
        }
      }
      if (cached.isNotEmpty) return ThreadHistory(messages: cached);
      rethrow;
    }
  }

  /// Persist the current transcript of [chatId] locally (best-effort).
  Future<void> cacheThread(String chatId, List<ChatMessage> messages) =>
      chatCache.save(chatId, messages);

  /// Delete a conversation for real.
  ///
  /// Goes through the authenticated WebSocket (`session.delete` mutation):
  /// the HTTP route answers 405 by design. The gateway answers OK for both
  /// "deleted" and "blocked_by_automations", so the payload decides. On
  /// success every trace of the chat is purged locally (row, transcript cache,
  /// remembered chat id).
  Future<DeleteSessionResult> deleteSession(SessionSummary s,
      {bool deleteAutomations = false}) async {
    final sock = await ensureSocket();
    final result =
        await sock.deleteSession(s.key, deleteAutomations: deleteAutomations);
    if (result.deleted) {
      sock.dropChat(s.chatId);
      sessions.removeWhere((x) => x.key == s.key);
      await chatCache.delete(s.chatId);
      if (lastChatId == s.chatId) {
        lastChatId = null;
        await _storage.delete(key: _kLastChat);
      }
      await _storeSessionCache(sessions);
      notifyListeners();
    }
    return result;
  }

  /// Automations attached to a chat — explains a blocked delete.
  Future<List<SessionAutomation>> sessionAutomations(SessionSummary s) async {
    if (_apiToken == null) return const [];
    try {
      return await api.fetchSessionAutomations(_apiToken!, s.key,
          supabaseToken: accessToken);
    } catch (_) {
      return const [];
    }
  }

  void rememberChat(String chatId) {
    lastChatId = chatId;
    unawaited(_storage.write(key: _kLastChat, value: chatId));
  }

  // ---- Chat socket (shared, auto-reconnecting) ---------------------------

  Future<NanobotSocket> ensureSocket() async {
    if (_socket != null && _socket!.isConnected) return _socket!;
    if (_wsToken == null || _wsPath == null) {
      await _bootstrapGateway();
    }
    final sock = _socket ??= NanobotSocket(tokenProvider: () async {
      // Always hand the socket a FRESH token: gateway tokens are minutes-old
      // by the time a reconnect happens.
      await rebootstrap();
      if (_wsToken == null) throw Exception('No gateway token available');
      return WsToken(_wsToken!, _wsPath ?? '/', supabaseToken: accessToken);
    });
    // Durable send queue: a task typed before the app was killed is re-sent
    // when the connection returns, and a task that reached the server keeps
    // running while the screen is closed.
    sock.pendingSends = pendingSends;
    sock.onConnectionChanged = (connected) {
      socketConnected = connected;
      if (connected) {
        // Re-pull session list & credits opportunistically after reconnect.
        unawaited(loadSessions());
      }
      notifyListeners();
    };
    sock.onSessionsChanged = () => unawaited(loadSessions());
    sock.onModelUpdated = (m) {
      modelName = m;
      notifyListeners();
    };
    if (!sock.isConnected) {
      try {
        await sock.connect();
      } catch (_) {
        // Offline / gateway down: keep the socket object. Outbound frames are
        // queued and flushed on the automatic reconnect, so a message typed
        // during a blip is not lost mid-task.
        socketStatus = AppSocketStatus.error;
      }
    }
    return sock;
  }

  /// Remember which chat the user has open so a task that is still running
  /// when the app is killed resumes streaming on the next launch.
  void rememberOpenChat(String chatId, {String? sessionKey}) {
    _socket?.setOpenChat(chatId, sessionKey: sessionKey);
    rememberChat(chatId);
    unawaited(_storage.write(
      key: _kOpenChat,
      value: jsonEncode({
        'chat_id': chatId,
        if (sessionKey != null) 'session_key': sessionKey,
      }),
    ));
  }

  /// The chat that was open when the app last stopped.
  Future<OpenChat?> _restoreOpenChat() async {
    try {
      final raw = await _storage.read(key: _kOpenChat);
      if (raw == null || raw.isEmpty) return null;
      final decoded = jsonDecode(raw);
      if (decoded is! Map) return null;
      final chatId = decoded['chat_id'];
      if (chatId is! String || chatId.isEmpty) return null;
      final sessionKey = decoded['session_key'];
      return OpenChat(
        chatId: chatId,
        sessionKey: sessionKey is String && sessionKey.isNotEmpty
            ? sessionKey
            : null,
      );
    } catch (_) {
      return null;
    }
  }

  /// Reconnect and re-subscribe the chat that was open, then flush any task
  /// that never reached the gateway. Idempotent; safe to call on every resume.
  ///
  /// This is what makes "give a task, close the app, come back" show the task
  /// still progressing — with its steps and its answer — instead of a frozen
  /// transcript.
  Future<void> resumeOpenChat() async {
    if (status != AppStatus.authenticated) return;
    final sock = await ensureSocket();
    final saved = await _restoreOpenChat();
    final target = saved?.chatId ?? sock.lastActedChatId ?? lastChatId;
    if (target == null) return;
    sock.setOpenChat(target, sessionKey: saved?.sessionKey);
    await sock.restore(chatId: target);
    socketConnected = sock.isConnected;
    notifyListeners();
  }

  void _fail(String msg) {
    errorMessage = msg;
    status = AppStatus.error;
    notifyListeners();
  }

  @override
  void dispose() {
    _tokenTimer?.cancel();
    _socket?.close();
    super.dispose();
  }
}
