import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';

import '../models.dart';
import '../services/gateway_api.dart';
import '../services/nanobot_socket.dart';
import '../services/supabase_auth.dart';

enum AppStatus { loading, unauthenticated, authenticating, authenticated, error }

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

  AppStatus status = AppStatus.loading;
  String? errorMessage;

  SupabaseAuth? _auth;
  String? accessToken;
  String? refreshToken;
  String? email;
  String? displayName;

  // Gateway bootstrap material
  String? _apiToken;
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
      _fail('Cannot reach PowerX service: $e');
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
      } catch (_) {
        try {
          await _refreshSupabase();
          await _bootstrapGateway();
          status = AppStatus.authenticated;
          notifyListeners();
          unawaited(loadSessions());
          unawaited(refreshCredits());
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
    status = AppStatus.authenticating;
    errorMessage = null;
    _resetIdentity();
    notifyListeners();
    try {
      final s = await _auth!.signUp(em.trim(), pw, name, referral: referral);
      if (s == null) {
        status = AppStatus.unauthenticated;
        errorMessage = 'Check your email to confirm your account, then sign in.';
        notifyListeners();
        return false;
      }
      await _persist(s, overwriteIdentity: true);
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
    if (_apiToken == null) return;
    sessionsLoading = true;
    notifyListeners();
    try {
      sessions = await api.listSessions(_apiToken!, supabaseToken: accessToken);
    } on ApiException catch (e) {
      if (e.status == 401) {
        await rebootstrap();
        try {
          sessions = await api.listSessions(_apiToken!, supabaseToken: accessToken);
        } catch (_) {}
      }
    } catch (_) {
      // keep previous list
    } finally {
      sessionsLoading = false;
      notifyListeners();
    }
  }

  Future<ThreadHistory> openSession(SessionSummary s) async {
    if (_apiToken == null) return ThreadHistory(messages: []);
    try {
      return await api.fetchThread(_apiToken!, s.key, supabaseToken: accessToken);
    } on ApiException catch (e) {
      if (e.status == 401) {
        await rebootstrap();
        return api.fetchThread(_apiToken!, s.key, supabaseToken: accessToken);
      }
      rethrow;
    }
  }

  Future<void> deleteSession(SessionSummary s) async {
    if (_apiToken == null) return;
    await api.deleteSession(_apiToken!, s.key, supabaseToken: accessToken);
    sessions.removeWhere((x) => x.key == s.key);
    notifyListeners();
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
      return WsToken(_wsToken!, _wsPath ?? '/');
    });
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
      await sock.connect();
    }
    return sock;
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
