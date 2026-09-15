import 'dart:convert';

import 'package:http/http.dart' as http;

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
  Future<SupabaseSession?> signUp(
      String email, String password, String name) async {
    final res = await _client.post(
      _base.replace(path: '/auth/v1/signup'),
      headers: _headers,
      body: jsonEncode({
        'email': email,
        'password': password,
        'data': {'name': name.trim().isEmpty ? 'PowerX User' : name.trim()},
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
  final String? email;
  final String? name;

  SupabaseSession({
    required this.accessToken,
    required this.refreshToken,
    this.expiresAt,
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
      email: email,
      name: name,
    );
  }

  bool get expiredSoon {
    if (expiresAt == null) return false;
    return expiresAt! <= DateTime.now().millisecondsSinceEpoch ~/ 1000 + 60;
  }
}

class AuthException implements Exception {
  final String message;
  AuthException(this.message);
  @override
  String toString() => message;
}
