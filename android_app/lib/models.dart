/// Data models for the PowerX native client.
library;

enum Role { user, assistant }

/// A single in-progress "step" the agent emits while working on a turn —
/// mirrors the WebUI's activity timeline (tool hints + tool_events).
class ActivityStep {
  final String id;
  final String name; // tool name or hint text
  final String detail; // short argument summary
  /// start | running | done | error
  String status;
  final DateTime startedAt;
  int order;

  ActivityStep({
    required this.id,
    required this.name,
    this.detail = '',
    this.status = 'running',
    DateTime? startedAt,
    this.order = 0,
  }) : startedAt = startedAt ?? DateTime.now();

  bool get isDone => status == 'done' || status == 'error';

  String get iconKey {
    final n = name.toLowerCase();
    if (n.contains('write') || n.contains('edit') || n.contains('create')) {
      return 'write';
    }
    if (n.contains('search') || n.contains('web') || n.contains('fetch') ||
        n.contains('browse')) {
      return 'search';
    }
    if (n.contains('exec') || n.contains('run') || n.contains('shell') ||
        n.contains('command') || n.contains('terminal')) {
      return 'run';
    }
    if (n.contains('image') || n.contains('generate')) {
      return 'image';
    }
    if (n == 'list' || n.startsWith('list_') || n.contains('list_dir') ||
        n == 'ls' || n.contains('ls_') || n.contains('directory')) {
      return 'list';
    }
    if (n.contains('read') || n.contains('open') || n.contains('file')) {
      return 'read';
    }
    return 'generic';
  }
}

/// An outbound attachment selected by the user before sending. Images are
/// encoded as base64 data URLs; other files are uploaded to onlyfiles.com and
/// referenced by URL (mirrors the WebUI's OutboundMedia wire shape).
class PendingAttachment {
  final String id;
  final String name;
  final String kind; // image | video | file
  final int sizeBytes;
  /// base64 data url for images/videos (set when ready).
  String? dataUrl;
  /// https url for non-image files (set after upload).
  String? url;
  /// uploading | ready | error
  String status;
  String? errorText;
  /// Local filesystem path kept for preview display.
  String? localPath;

  PendingAttachment({
    required this.id,
    required this.name,
    required this.kind,
    this.sizeBytes = 0,
    this.dataUrl,
    this.url,
    this.status = 'uploading',
    this.errorText,
    this.localPath,
  });

  bool get isReady => status == 'ready';
  bool get isError => status == 'error';

  Map<String, dynamic> toWireMedia() {
    if (dataUrl != null && dataUrl!.isNotEmpty) {
      return {'data_url': dataUrl, if (name.isNotEmpty) 'name': name};
    }
    if (url != null && url!.isNotEmpty) {
      return {'url': url, if (name.isNotEmpty) 'name': name};
    }
    return {};
  }
}

class ChatMessage {
  final String id;
  final Role role;
  String text;
  /// Assistant reasoning / thinking stream (optional).
  String reasoning;
  bool streaming;
  final DateTime createdAt;
  /// Media paths attached to a message (best-effort display).
  List<String> media;
  /// Ordered activity steps shown while the assistant works on this turn.
  final List<ActivityStep> activity;
  /// Whether this turn ended with an error breadcrumb.
  bool hasError;

  ChatMessage({
    required this.id,
    required this.role,
    this.text = '',
    this.reasoning = '',
    this.streaming = false,
    DateTime? createdAt,
    List<String>? media,
    List<ActivityStep>? activity,
    this.hasError = false,
  })  : createdAt = createdAt ?? DateTime.now(),
        media = media ?? [],
        activity = activity ?? [];

  bool get isEmpty =>
      text.trim().isEmpty && reasoning.trim().isEmpty && activity.isEmpty;

  /// Attachments that can be rendered inline (http(s) urls only).
  List<String> get viewableMedia =>
      media.where((m) => m.startsWith('http')).toList();
}

class SessionSummary {
  final String key; // e.g. "websocket:<chat_id>"
  final String chatId;
  final String title;
  final String preview;
  final DateTime? updatedAt;

  SessionSummary({
    required this.key,
    required this.chatId,
    required this.title,
    required this.preview,
    this.updatedAt,
  });

  factory SessionSummary.fromJson(Map<String, dynamic> json) {
    final key = (json['key'] ?? '') as String;
    var chatId = key;
    final idx = key.indexOf(':');
    if (idx >= 0) chatId = key.substring(idx + 1);
    return SessionSummary(
      key: key,
      chatId: chatId,
      title: ((json['title'] ?? '') as String).trim(),
      preview: ((json['preview'] ?? '') as String).trim(),
      updatedAt: DateTime.tryParse((json['updated_at'] ?? '') as String),
    );
  }

  String get displayTitle => title.isNotEmpty ? title : 'New chat';
}

/// Credit balance breakdown from the Supabase profiles table.
class CreditInfo {
  final int total;
  final int daily;
  final int purchased;
  final int granted;
  final int drainRate;

  const CreditInfo({
    required this.total,
    required this.daily,
    required this.purchased,
    required this.granted,
    required this.drainRate,
  });

  static CreditInfo? fromRow(Map<String, dynamic> row) {
    final daily = _asInt(row['daily_credits']);
    final purchased = _asInt(row['purchased_credits']);
    final granted = _asInt(row['granted_credits']);
    final drain = _asInt(row['drain_rate'], fallback: 1);
    return CreditInfo(
      total: daily + purchased + granted,
      daily: daily,
      purchased: purchased,
      granted: granted,
      drainRate: drain.clamp(1, 999),
    );
  }

  static int _asInt(dynamic v, {int fallback = 0}) {
    if (v is int) return v;
    if (v is double) return v.round();
    if (v is String) return int.tryParse(v) ?? fallback;
    return fallback;
  }
}

/// A purchasable credit package returned by /webui/bootstrap.
class PaymentPackage {
  final String name;
  final String slug;
  final int credits;
  final double amountUsd;

  const PaymentPackage({
    required this.name,
    required this.slug,
    required this.credits,
    required this.amountUsd,
  });

  factory PaymentPackage.fromJson(Map<String, dynamic> j) => PaymentPackage(
        name: (j['name'] ?? '') as String,
        slug: (j['slug'] ?? '') as String,
        credits: (j['credits'] is num) ? (j['credits'] as num).toInt() : 0,
        amountUsd: (j['amount_usd'] is num) ? (j['amount_usd'] as num).toDouble() : 0,
      );
}

/// A persisted turn from /api/sessions/{key}/webui-thread
class ThreadTurn {
  final String role; // "user" | "assistant"
  final String content;
  final String? reasoning;
  final List<String> media;
  /// Intermediate activity breadcrumbs replayed from the transcript.
  final List<ActivityStep> activity;

  ThreadTurn({
    required this.role,
    required this.content,
    this.reasoning,
    this.media = const [],
    List<ActivityStep>? activity,
  }) : activity = activity ?? [];

  static List<ThreadTurn> parseWebuiThread(dynamic payload) {
    final out = <ThreadTurn>[];
    if (payload is! Map) return out;
    final turns = payload['turns'];
    if (turns is! List) {
      // Some gateways use messages[]
      final msgs = payload['messages'];
      if (msgs is List) {
        for (final m in msgs) {
          if (m is Map) {
            final r = (m['role'] ?? '').toString();
            final c = (m['content'] ?? m['text'] ?? '').toString();
            if (r == 'user' || r == 'assistant') {
              out.add(ThreadTurn(role: r, content: c));
            }
          }
        }
      }
      return out;
    }
    for (final t in turns) {
      if (t is! Map) continue;
      final userText = _firstString(t['user']) ?? '';
      final assistantText = _firstString(t['assistant']) ?? '';
      final reason = t['reasoning'] is String ? t['reasoning'] as String : null;
      final media = <String>[];
      final rawMedia = t['media'];
      if (rawMedia is List) {
        for (final m in rawMedia) {
          if (m is String) media.add(m);
        }
      }
      if (userText.isNotEmpty) {
        out.add(ThreadTurn(role: 'user', content: userText));
      }
      if (assistantText.isNotEmpty || reason != null) {
        out.add(ThreadTurn(
          role: 'assistant',
          content: assistantText,
          reasoning: reason,
          media: media,
        ));
      }
    }
    return out;
  }

  static String? _firstString(dynamic v) {
    if (v is String) return v;
    if (v is Map) {
      final c = v['content'] ?? v['text'];
      if (c is String) return c;
    }
    return null;
  }
}
