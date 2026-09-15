import 'dart:convert';

import 'package:http/http.dart' as http;

bool _isOk(http.Response r) => r.statusCode >= 200 && r.statusCode < 300;

/// Minimal Supabase Auth client over REST — no SDK dependency.
///
/// The PowerX gateway exposes its Supabase URL + anon key through
/// `/webui/bootstrap`, so nothing is baked into the app.
class SupabaseAuth {
  final String url;
  final String anonKey;
  final http.Client _client;

  SupabaseAuth({required this.url, required this.anonKey, http.Client? client})
      : _client = client ?? http.Client();

  Map<String, String> get _headers => {
        'apikey': anonKey,
        'Content-Type': 'application/json',
      };

  Uri get _base => Uri.parse(url.replaceAll(RegExp(r'/+$'), ''));

  /// Email/password sign-in. Returns a session map or throws [AuthException].
  Future<SupabaseSession> signIn(String email, String password) async {
    final res = await _client.post(
      _base.replace(path: '/auth/v1/token', query: 'grant_type=password'),
      headers: _headers,
      body: jsonEncode({'email': email, 'password': password}),
    );
    if (res.statusCode != 200) {
      throw AuthException(_message(res));
    }
    return SupabaseSession.fromJson(jsonDecode(res.body) as Map<String, dynamic>);
  }

  /// Email/password sign-up. May require email confirmation (returns null session).
  ///
  /// [referral] is the referrer's email address; when provided it is stored on
  /// the new user's metadata and later redeemed via [claimReferral].
  Future<SupabaseSession?> signUp(
      String email, String password, String name,
      {String? referral}) async {
    final data = <String, dynamic>{
      'name': name.trim().isEmpty ? 'CDNAI User' : name.trim(),
      'role': 'user',
      'source': 'android',
    };
    final ref = (referral ?? '').trim();
    if (ref.isNotEmpty) {
      data['referral'] = ref;
    }
    final res = await _client.post(
      _base.replace(path: '/auth/v1/signup'),
      headers: _headers,
      body: jsonEncode({
        'email': email,
        'password': password,
        'data': data,
      }),
    );
    if (res.statusCode != 200) {
      throw AuthException(_message(res));
    }
    final body = jsonDecode(res.body) as Map<String, dynamic>;
    if (body['access_token'] == null) {
      // Confirmation required.
      return null;
    }
    return SupabaseSession.fromJson(body);
  }

  /// Refresh an access token using the stored refresh token.
  Future<SupabaseSession> refresh(String refreshToken) async {
    final res = await _client.post(
      _base.replace(path: '/auth/v1/token', query: 'grant_type=refresh_token'),
      headers: {..._headers, 'Authorization': 'Bearer $refreshToken'},
      body: jsonEncode({'refresh_token': refreshToken}),
    );
    if (res.statusCode != 200) {
      throw AuthException(_message(res));
    }
    return SupabaseSession.fromJson(jsonDecode(res.body) as Map<String, dynamic>);
  }

  /// Claim the one-time 700-credit referral bonus for a freshly created account.
  /// Must be called right after signUp with the NEW user's access token.
  Future<ReferralResult> claimReferral(
      String accessToken, String referral) async {
    final ref = referral.trim();
    if (ref.isEmpty) return const ReferralResult(ok: false, error: 'No code');
    try {
      final res = await _client.post(
        _base.replace(path: '/functions/v1/referral-claim'),
        headers: {
          ..._headers,
          'Authorization': 'Bearer $accessToken',
        },
        body: jsonEncode({'referral': ref}),
      );
      Map<String, dynamic>? payload;
      try {
        payload = jsonDecode(res.body) as Map<String, dynamic>;
      } catch (_) {
        payload = null;
      }
      if (!_isOk(res) || payload?['ok'] != true) {
        return ReferralResult(
          ok: false,
          error: (payload?['error'] ?? 'Referral claim failed').toString(),
        );
      }
      final credits = payload?['credits'];
      return ReferralResult(
        ok: true,
        credits: credits is num ? credits.toInt() : null,
      );
    } catch (e) {
      return ReferralResult(ok: false, error: '$e');
    }
  }

  /// Fetch the signed-in user's credit balance from the profiles table (RLS).
  Future<CreditBundle?> fetchCredits(String accessToken) async {
    try {
      final res = await _client.get(
        _base.replace(
          path: '/rest/v1/profiles',
          query:
              'select=daily_credits%2Cpurchased_credits%2Cgranted_credits%2Cdrain_rate&limit=1',
        ),
        headers: {'apikey': anonKey, 'Authorization': 'Bearer $accessToken'},
      );
      if (!_isOk(res)) return null;
      final rows = jsonDecode(res.body);
      if (rows is! List || rows.isEmpty) return null;
      final row = Map<String, dynamic>.from(rows.first as Map);
      int n(dynamic v, {int fallback = 0}) {
        if (v is int) return v;
        if (v is double) return v.round();
        if (v is String) return int.tryParse(v) ?? fallback;
        return fallback;
      }

      final daily = n(row['daily_credits']);
      final purchased = n(row['purchased_credits']);
      final granted = n(row['granted_credits']);
      final drain = n(row['drain_rate'], fallback: 1).clamp(1, 999);
      return CreditBundle(
        total: daily + purchased + granted,
        daily: daily,
        purchased: purchased,
        granted: granted,
        drainRate: drain,
      );
    } catch (_) {
      return null;
    }
  }

  /// Check whether the current user's own referral code has been used yet.
  Future<bool?> referralUsed(String accessToken, String email) async {
    try {
      final code = Uri.encodeComponent(email.trim().toLowerCase());
      final res = await _client.get(
        _base.replace(
          path: '/rest/v1/referrals',
          query: 'code=eq.$code&select=used_at&limit=1',
        ),
        headers: {'apikey': anonKey, 'Authorization': 'Bearer $accessToken'},
      );
      if (!_isOk(res)) return null;
      final rows = jsonDecode(res.body);
      if (rows is! List || rows.isEmpty) return null;
      final usedAt = (rows.first as Map)['used_at'];
      return usedAt != null && usedAt.toString().isNotEmpty;
    } catch (_) {
      return null;
    }
  }

  /// Verify a Flutterwave payment by reference (pay-verify edge function).
  Future<VerifyPaymentResult> verifyPayment(
      String accessToken, String txRef,
      {String? transactionId}) async {
    final ref = txRef.trim();
    if (ref.isEmpty) {
      return const VerifyPaymentResult(ok: false, error: 'A transaction reference is required.');
    }
    final body = <String, dynamic>{
      'tx_ref': ref.length > 300 ? ref.substring(0, 300) : ref,
    };
    final txn = (transactionId ?? '').trim();
    if (txn.isNotEmpty) {
      body['transaction_id'] = txn.length > 100 ? txn.substring(0, 100) : txn;
    }
    try {
      final res = await _client.post(
        _base.replace(path: '/functions/v1/pay-verify'),
        headers: {..._headers, 'Authorization': 'Bearer $accessToken'},
        body: jsonEncode(body),
      );
      Map<String, dynamic>? payload;
      try {
        payload = jsonDecode(res.body) as Map<String, dynamic>;
      } catch (_) {
        payload = null;
      }
      if (!_isOk(res)) {
        final err = payload?['error'];
        return VerifyPaymentResult(
          ok: false,
          error: (err ?? 'Payment verification failed (HTTP ${res.statusCode})')
              .toString(),
        );
      }
      if (payload?['ok'] != true) {
        return VerifyPaymentResult(
          ok: false,
          error: (payload?['error'] ?? 'Payment verification failed').toString(),
        );
      }
      final credits = payload?['credits'];
      final pkg = payload?['pkg'];
      return VerifyPaymentResult(
        ok: true,
        credits: credits is num ? credits.toInt() : null,
        pkg: pkg is String ? pkg : null,
      );
    } catch (e) {
      return VerifyPaymentResult(ok: false, error: '$e');
    }
  }

  static String _message(http.Response res) {
    try {
      final b = jsonDecode(res.body);
      if (b is Map) {
        return (b['msg'] ?? b['error_description'] ?? b['error'] ?? 'HTTP ${res.statusCode}')
            .toString();
      }
    } catch (_) {}
    return 'HTTP ${res.statusCode}';
  }
}

class SupabaseSession {
  final String accessToken;
  final String refreshToken;
  final int? expiresAt;
  final int? expiresIn;
  final String? email;
  final String? name;

  SupabaseSession({
    required this.accessToken,
    required this.refreshToken,
    this.expiresAt,
    this.expiresIn,
    this.email,
    this.name,
  });

  factory SupabaseSession.fromJson(Map<String, dynamic> j) {
    final user = j['user'];
    String? email;
    String? name;
    if (user is Map) {
      email = user['email'] as String?;
      final meta = user['user_metadata'];
      if (meta is Map) name = meta['name'] as String?;
    }
    return SupabaseSession(
      accessToken: j['access_token'] as String,
      refreshToken: j['refresh_token'] as String,
      expiresAt: j['expires_at'] is int ? j['expires_at'] as int : null,
      expiresIn: j['expires_in'] is num ? (j['expires_in'] as num).toInt() : null,
      email: email,
      name: name,
    );
  }

  bool get expiredSoon {
    if (expiresAt == null) return false;
    return expiresAt! <= DateTime.now().millisecondsSinceEpoch ~/ 1000 + 60;
  }
}

class CreditBundle {
  final int total;
  final int daily;
  final int purchased;
  final int granted;
  final int drainRate;
  const CreditBundle({
    required this.total,
    required this.daily,
    required this.purchased,
    required this.granted,
    required this.drainRate,
  });
}

class ReferralResult {
  final bool ok;
  final int? credits;
  final String? error;
  const ReferralResult({required this.ok, this.credits, this.error});
}

class VerifyPaymentResult {
  final bool ok;
  final int? credits;
  final String? pkg;
  final String? error;
  const VerifyPaymentResult({required this.ok, this.credits, this.pkg, this.error});
}

class AuthException implements Exception {
  final String message;
  AuthException(this.message);
  @override
  String toString() => message;
}
