import 'dart:convert';

import 'package:http/http.dart' as http;

import '../config.dart';
import '../models.dart';

/// Result of `/webui/bootstrap` for an authenticated user.
class GatewayBootstrap {
  final String token; // WebSocket connection token
  final String apiToken; // REST bearer token
  final String wsPath;
  final bool needsAuth;
  final String? supabaseUrl;
  final String? supabaseAnonKey;

  GatewayBootstrap({
    required this.token,
    required this.apiToken,
    required this.wsPath,
    this.needsAuth = false,
    this.supabaseUrl,
    this.supabaseAnonKey,
  });

  factory GatewayBootstrap.fromJson(Map<String, dynamic> j) {
    final needsAuth = j['needs_auth'] != null && j['needs_auth'] != false;
    String? sbUrl, sbKey;
    final sb = j['supabase'];
    if (sb is Map) {
      sbUrl = sb['url'] as String?;
      sbKey = sb['anon_key'] as String?;
    }
    return GatewayBootstrap(
      token: (j['token'] ?? '') as String,
      apiToken: (j['api_token'] ?? j['token'] ?? '') as String,
      wsPath: (j['ws_path'] ?? '/') as String,
      needsAuth: needsAuth,
      supabaseUrl: sbUrl,
      supabaseAnonKey: sbKey,
    );
  }
}

class ApiException implements Exception {
  final int status;
  final String message;
  ApiException(this.status, this.message);
  @override
  String toString() => message;
}

/// Thin REST client for the PowerX gateway surface used by the native app.
class GatewayApi {
  final http.Client _client;
  final String origin;

  GatewayApi({http.Client? client, String? origin})
      : _client = client ?? http.Client(),
        origin = origin ?? PowerXConfig.origin;

  /// Fetch bootstrap. Pass [supabaseAccessToken] once signed in to exchange it
  /// for a gateway WS/REST token. Without it, returns needs_auth + Supabase config.
  Future<GatewayBootstrap> bootstrap({String? supabaseAccessToken}) async {
    final res = await _client.get(
      Uri.parse('$origin/webui/bootstrap'),
      headers: {
        if (supabaseAccessToken != null && supabaseAccessToken.isNotEmpty)
          'X-Nanobot-Auth': supabaseAccessToken,
      },
    );
    if (res.statusCode == 401 || res.statusCode == 403) {
      throw ApiException(res.statusCode, 'Authentication required');
    }
    if (res.statusCode != 200) {
      throw ApiException(res.statusCode, 'bootstrap failed: HTTP ${res.statusCode}');
    }
    return GatewayBootstrap.fromJson(jsonDecode(res.body) as Map<String, dynamic>);
  }

  Future<List<SessionSummary>> listSessions(String apiToken) async {
    final res = await _client.get(
      Uri.parse('$origin/api/sessions'),
      headers: {'Authorization': 'Bearer $apiToken'},
    );
    if (res.statusCode != 200) {
      throw ApiException(res.statusCode, 'Could not load sessions');
    }
    final body = jsonDecode(res.body) as Map<String, dynamic>;
    final rows = (body['sessions'] ?? []) as List;
    return rows
        .map((r) => SessionSummary.fromJson(r as Map<String, dynamic>))
        .toList();
  }

  Future<List<ThreadTurn>> fetchThread(String apiToken, String key) async {
    final url =
        '$origin/api/sessions/${Uri.encodeComponent(key)}/webui-thread?limit=200&direction=latest';
    final res = await _client.get(
      Uri.parse(url),
      headers: {'Authorization': 'Bearer $apiToken', 'Cache-Control': 'no-store'},
    );
    if (res.statusCode == 404) return [];
    if (res.statusCode != 200) {
      throw ApiException(res.statusCode, 'Could not load conversation');
    }
    return ThreadTurn.parseWebuiThread(jsonDecode(res.body));
  }
}
