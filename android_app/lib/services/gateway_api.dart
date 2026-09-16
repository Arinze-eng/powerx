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
  final int? expiresInSeconds;
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
    this.expiresInSeconds,
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
      expiresInSeconds:
          j['expires_in'] is num ? (j['expires_in'] as num).toInt() : null,
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

/// Outcome of a session delete. The gateway answers 200 even when it refuses
/// to delete (see [blockedByAutomations]), so the status code alone is not
/// enough — this captures the real business result.
class DeleteSessionResult {
  /// The session/transcript is gone (true) or still present (false).
  final bool deleted;
  /// The gateway refused because automations are attached to the session.
  final bool blockedByAutomations;
  /// Human-readable names of the blocking automations.
  final List<String> automations;
  const DeleteSessionResult({
    required this.deleted,
    this.blockedByAutomations = false,
    this.automations = const [],
  });

  factory DeleteSessionResult.fromJson(Map<String, dynamic> j) {
    final names = <String>[];
    final jobs = j['automations'];
    if (jobs is List) {
      for (final job in jobs) {
        if (job is Map) {
          final label = (job['name'] ?? job['title'] ?? job['id'] ?? '').toString();
          if (label.trim().isNotEmpty) names.add(label.trim());
        }
      }
    }
    return DeleteSessionResult(
      deleted: j['deleted'] == true,
      blockedByAutomations: j['blocked_by_automations'] == true,
      automations: names,
    );
  }
}

/// A scheduled automation attached to a chat session.
class SessionAutomation {
  final String id;
  final String name;
  final String schedule;
  final bool enabled;
  final bool pending;
  const SessionAutomation({
    required this.id,
    required this.name,
    this.schedule = '',
    this.enabled = true,
    this.pending = false,
  });

  factory SessionAutomation.fromJson(Map<String, dynamic> j) => SessionAutomation(
        id: (j['id'] ?? j['job_id'] ?? '').toString(),
        name: (j['name'] ?? j['title'] ?? '').toString(),
        schedule: (j['schedule'] ?? j['cron'] ?? '').toString(),
        enabled: j['enabled'] != false,
        pending: j['pending'] == true,
      );

  String get displayName => name.trim().isNotEmpty ? name.trim() : 'Automation';
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

  Future<List<SessionSummary>> listSessions(String apiToken,
      {String? supabaseToken}) async {
    final res = await _client.get(
      Uri.parse('$origin/api/sessions'),
      headers: {
        'Authorization': 'Bearer $apiToken',
        // Required in Supabase mode: resolves the chat owner for per-user
        // isolation. Without it the server fails closed with an empty list.
        if (supabaseToken != null) 'X-Nanobot-Auth': supabaseToken,
      },
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

  Future<ThreadHistory> fetchThread(String apiToken, String key,
      {String? supabaseToken}) async {
    final url =
        '$origin/api/sessions/${Uri.encodeComponent(key)}/webui-thread?limit=200&direction=latest';
    final res = await _client.get(
      Uri.parse(url),
      headers: {
        'Authorization': 'Bearer $apiToken',
        'Cache-Control': 'no-store',
        if (supabaseToken != null) 'X-Nanobot-Auth': supabaseToken,
      },
    );
    if (res.statusCode == 404) return ThreadHistory(messages: []);
    if (res.statusCode != 200) {
      throw ApiException(res.statusCode, 'Could not load conversation');
    }
    return ThreadHistory.parse(jsonDecode(res.body));
  }

  /// Delete a session and its transcript from the server.
  ///
  /// The gateway returns HTTP 200 with `{"deleted": false,
  /// "blocked_by_automations": true}` when scheduled automations are attached.
  /// The caller must inspect the payload — treating a 200 as success is what
  /// made "delete" appear to do nothing while the row bounced back.
  ///
  /// [deleteAutomations] force-deletes those attached automations too.
  Future<DeleteSessionResult> deleteSession(
    String apiToken,
    String key, {
    String? supabaseToken,
    bool deleteAutomations = false,
  }) async {
    final url = Uri.parse(
        '$origin/api/sessions/${Uri.encodeComponent(key)}/delete'
        '${deleteAutomations ? '?delete_automations=1' : ''}');
    final res = await _client.post(
      url,
      headers: {
        'Authorization': 'Bearer $apiToken',
        if (supabaseToken != null) 'X-Nanobot-Auth': supabaseToken,
      },
    );
    if (res.statusCode != 200 && res.statusCode != 204) {
      throw ApiException(res.statusCode, 'Could not delete conversation');
    }
    if (res.body.trim().isEmpty) {
      return const DeleteSessionResult(deleted: true);
    }
    try {
      final body = jsonDecode(res.body);
      if (body is Map) {
        return DeleteSessionResult.fromJson(Map<String, dynamic>.from(body));
      }
    } catch (_) {
      // Non-JSON 200 → treat as success (older gateway builds).
    }
    return const DeleteSessionResult(deleted: true);
  }

  /// Automations (cron jobs / local triggers) attached to a session. Used to
  /// explain WHY a delete was refused instead of failing silently.
  Future<List<SessionAutomation>> fetchSessionAutomations(
    String apiToken,
    String key, {
    String? supabaseToken,
  }) async {
    final res = await _client.get(
      Uri.parse('$origin/api/sessions/${Uri.encodeComponent(key)}/automations'),
      headers: {
        'Authorization': 'Bearer $apiToken',
        if (supabaseToken != null) 'X-Nanobot-Auth': supabaseToken,
      },
    );
    if (res.statusCode != 200) {
      throw ApiException(res.statusCode, 'Could not load automations');
    }
    final body = jsonDecode(res.body);
    if (body is! Map) return const [];
    final jobs = body['jobs'];
    if (jobs is! List) return const [];
    return jobs
        .whereType<Map>()
        .map((j) => SessionAutomation.fromJson(Map<String, dynamic>.from(j)))
        .toList();
  }

  /// Running gateway build info (`/api/version`). Best-effort: used by
  /// Settings to show which backend the app is actually talking to.
  Future<Map<String, dynamic>> fetchVersion() async {
    final res = await _client.get(Uri.parse('$origin/api/version'));
    if (res.statusCode != 200) {
      throw ApiException(res.statusCode, 'Could not load server version');
    }
    final body = jsonDecode(res.body);
    return body is Map ? Map<String, dynamic>.from(body) : const {};
  }

  /// Fetch a text preview of a workspace file created during a chat
  /// (gateway file-preview endpoint, same auth as the session APIs).
  ///
  /// Throws [ApiException] with the real status so the UI can explain WHY a
  /// file could not be opened (404 missing, 403 outside workspace, 415 binary).
  Future<Map<String, dynamic>> fetchFilePreview(String apiToken, String key,
      {required String path, String? supabaseToken}) async {
    final url =
        '$origin/api/sessions/${Uri.encodeComponent(key)}/file-preview?path=${Uri.encodeQueryComponent(path)}';
    final res = await _client.get(
      Uri.parse(url),
      headers: {
        'Authorization': 'Bearer $apiToken',
        'Cache-Control': 'no-store',
        if (supabaseToken != null) 'X-Nanobot-Auth': supabaseToken,
      },
    );
    if (res.statusCode != 200) {
      String detail = 'Could not download file';
      try {
        final body = jsonDecode(res.body);
        if (body is Map) {
          final msg = body['message'] ?? body['error'] ?? body['detail'];
          if (msg is String && msg.trim().isNotEmpty) detail = msg.trim();
        }
      } catch (_) {}
      throw ApiException(res.statusCode, detail);
    }
    return jsonDecode(res.body) as Map<String, dynamic>;
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
