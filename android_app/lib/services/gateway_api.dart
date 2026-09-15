import 'dart:async';
import 'dart:convert';
import 'dart:io';

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
  final String? modelName;
  final String? userEmail;
  final String? supabaseUserId;
  final List<PaymentPackage> paymentPackages;
  final String paymentUrl;

  GatewayBootstrap({
    required this.token,
    required this.apiToken,
    required this.wsPath,
    this.needsAuth = false,
    this.supabaseUrl,
    this.supabaseAnonKey,
    this.modelName,
    this.userEmail,
    this.supabaseUserId,
    this.paymentPackages = const [],
    this.paymentUrl = '',
  });

  factory GatewayBootstrap.fromJson(Map<String, dynamic> j) {
    final needsAuth = j['needs_auth'] != null && j['needs_auth'] != false;
    String? sbUrl, sbKey;
    var packages = <PaymentPackage>[];
    var payUrl = '';
    final sb = j['supabase'];
    if (sb is Map) {
      sbUrl = sb['url'] as String?;
      sbKey = sb['anon_key'] as String?;
      final payment = sb['payment'];
      if (payment is Map) {
        payUrl = (payment['payment_url'] ?? '') as String;
        final pkgs = payment['packages'];
        if (pkgs is List) {
          packages = pkgs
              .whereType<Map>()
              .map((p) => PaymentPackage.fromJson(
                  Map<String, dynamic>.from(p)))
              .toList();
        }
      }
    }
    return GatewayBootstrap(
      token: (j['token'] ?? '') as String,
      apiToken: (j['api_token'] ?? j['token'] ?? '') as String,
      wsPath: (j['ws_path'] ?? '/') as String,
      needsAuth: needsAuth,
      supabaseUrl: sbUrl,
      supabaseAnonKey: sbKey,
      modelName: j['model_name'] as String?,
      userEmail: j['user_email'] as String?,
      supabaseUserId: j['supabase_user_id'] as String?,
      paymentPackages: packages,
      paymentUrl: payUrl,
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

  /// Delete a session/conversation from the server. Best-effort.
  Future<void> deleteSession(String apiToken, String key) async {
    final res = await _client.post(
      Uri.parse('$origin/api/sessions/${Uri.encodeComponent(key)}/delete'),
      headers: {'Authorization': 'Bearer $apiToken'},
    );
    if (res.statusCode != 200 && res.statusCode != 204) {
      throw ApiException(res.statusCode, 'Could not delete conversation');
    }
  }
}

/// Direct device -> onlyfiles.com uploads for non-image attachments, mirroring
/// the WebUI's browser-side upload path so file bytes never transit the host.
class OnlyFilesUploader {
  static const uploadUrl = 'https://onlyfiles.com/api/v1/upload';
  static const maxBytes = 100 * 1024 * 1024;

  /// Upload a local file and return its public page URL. Throws on failure.
  Future<String> upload(File file, {String? name}) async {
    final length = await file.length();
    if (length > maxBytes) {
      throw StateError('File exceeds the 100 MB limit.');
    }
    final req = http.MultipartRequest('POST', Uri.parse(uploadUrl));
    req.files.add(await http.MultipartFile.fromPath(
      'file',
      file.path,
      filename: name ?? file.uri.pathSegments.last,
    ));
    final streamed = await req.send().timeout(const Duration(seconds: 120));
    final res = await http.Response.fromStream(streamed);
    if (res.statusCode != 200) {
      throw StateError('Upload rejected (HTTP ${res.statusCode}).');
    }
    Map<String, dynamic>? payload;
    try {
      payload = jsonDecode(res.body) as Map<String, dynamic>;
    } catch (_) {
      payload = null;
    }
    final full = payload?['data']?['file']?['url']?['full'];
    if (full is! String || full.isEmpty) {
      throw StateError('Upload failed: no URL returned.');
    }
    return full;
  }
}
