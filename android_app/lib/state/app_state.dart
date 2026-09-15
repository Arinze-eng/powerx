import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';

import '../models.dart';
import '../services/gateway_api.dart';
import '../services/nanobot_socket.dart';
import '../services/supabase_auth.dart';

enum AppStatus { loading, unauthenticated, authenticating, authenticated, error }

/// Central app state: owns auth session, gateway tokens, the chat socket, and
/// the list of sessions. Persists tokens securely so sign-in survives restarts.
class AppState extends ChangeNotifier {
  final GatewayApi api = GatewayApi();
  final FlutterSecureStorage _storage = const FlutterSecureStorage();

  static const _kSbUrl = 'sb_url';
  static const _kSbKey = 'sb_key';
  static const _kAccess = 'access_token';
  static const _kRefresh = 'refresh_token';
  static const _kEmail = 'email';
  static const _kName = 'name';
  /// Last open chat id so a backgrounded turn can be resumed on reopen.
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
  NanobotSocket? _socket;

  // Bootstrap-derived profile/billing data
  String? modelName;
  String? supabaseUserId;
  List<PaymentPackage> paymentPackages = const [];
  String paymentUrl = '';

  // Credits (fetched lazily from Supabase profiles).
  CreditBundle? credits;
  bool creditsLoading = false;

  List<SessionSummary> sessions = [];
  bool sessionsLoading = false;

  String? lastChatId;

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
        // Token likely expired — refresh once.
        try {
          final s = await _auth!.refresh(rt);
          await _persist(s);
          await _bootstrapGateway();
          status = AppStatus.authenticated;
          notifyListeners();
          unawaited(loadSessions());
          unawaited(refreshCredits());
        } catch (e) {
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
    notifyListeners();
    try {
      final s = await _auth!.signIn(em.trim(), pw);
      await _persist(s);
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
    notifyListeners();
    try {
      final s = await _auth!.signUp(em.trim(), pw, name, referral: referral);
      if (s == null) {
        status = AppStatus.unauthenticated;
        errorMessage = 'Check your email to confirm your account, then sign in.';
        notifyListeners();
        return false;
      }
      await _persist(s);
      // Redeem the one-time referral bonus right after account creation.
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
    await _clearSession();
    sessions = [];
    credits = null;
    status = AppStatus.unauthenticated;
    notifyListeners();
  }

  Future<void> _persist(SupabaseSession s) async {
    accessToken = s.accessToken;
    refreshToken = s.refreshToken;
    email = s.email ?? email;
    displayName = s.name ?? displayName;
    await _storage.write(key: _kAccess, value: s.accessToken);
    await _storage.write(key: _kRefresh, value: s.refreshToken);
    if (s.email != null) await _storage.write(key: _kEmail, value: s.email!);
    if (s.name != null) await _storage.write(key: _kName, value: s.name!);
  }

  Future<void> _clearSession() async {
    accessToken = null;
    refreshToken = null;
    _apiToken = null;
    _wsToken = null;
    _wsPath = null;
    lastChatId = null;
    await _storage.delete(key: _kAccess);
    await _storage.delete(key: _kRefresh);
    await _storage.delete(key: _kLastChat);
  }

  /// Exchange the Supabase access token for a gateway WS/REST token.
  Future<void> _bootstrapGateway() async {
    final boot = await api.bootstrap(supabaseAccessToken: accessToken);
    if (boot.needsAuth || boot.token.isEmpty) {
      throw Exception('Gateway rejected authentication');
    }
    _apiToken = boot.apiToken;
    _wsToken = boot.token;
    _wsPath = boot.wsPath;
    modelName = boot.modelName;
    supabaseUserId = boot.supabaseUserId;
    paymentPackages = boot.paymentPackages;
    paymentUrl = boot.paymentUrl;
    if ((boot.userEmail ?? '').isNotEmpty) email = boot.userEmail;
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
      sessions = await api.listSessions(_apiToken!);
    } catch (_) {
      // keep previous list
    } finally {
      sessionsLoading = false;
      notifyListeners();
    }
  }

  Future<List<ThreadTurn>> openSession(SessionSummary s) async {
    if (_apiToken == null) return [];
    return api.fetchThread(_apiToken!, s.key);
  }

  Future<void> deleteSession(SessionSummary s) async {
    if (_apiToken == null) return;
    await api.deleteSession(_apiToken!, s.key);
    sessions.removeWhere((x) => x.key == s.key);
    notifyListeners();
  }

  void rememberChat(String chatId) {
    lastChatId = chatId;
    unawaited(_storage.write(key: _kLastChat, value: chatId));
  }

  // ---- Chat socket ------------------------------------------------------

  Future<NanobotSocket> ensureSocket() async {
    if (_socket != null && _socket!.isOpen) return _socket!;
    if (_wsToken == null || _wsPath == null) {
      await _bootstrapGateway();
    }
    final sock = NanobotSocket(token: _wsToken!, wsPath: _wsPath!);
    sock.onDisconnected = () {
      _socket = null;
      notifyListeners();
    };
    await sock.connect();
    _socket = sock;
    return sock;
  }

  void _fail(String msg) {
    errorMessage = msg;
    status = AppStatus.error;
    notifyListeners();
  }

  @override
  void dispose() {
    _socket?.close();
    super.dispose();
  }
}
