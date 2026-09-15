/// Data models for the PowerX native client.
library;

import 'dart:convert';

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

  /// Parse one live `tool_events[]` entry (phase start/end/error).
  static ActivityStep? fromToolEvent(Map<String, dynamic> te,
      {int order = 0, String fallbackId = ''}) {
    final phase = (te['phase'] ?? '').toString();
    final name = (te['name'] ?? te['tool'] ?? '').toString();
    final callId = (te['call_id'] ?? te['callId'] ?? '').toString();
    if (name.isEmpty) return null;
    final detail = summarizeArgs(te['arguments'] ?? te['args']);
    final status = phase == 'start'
        ? 'running'
        : phase == 'error'
            ? 'error'
            : 'done';
    return ActivityStep(
      id: callId.isNotEmpty
          ? callId
          : (fallbackId.isNotEmpty ? fallbackId : '$name-$order'),
      name: name,
      detail: detail,
      status: status,
      order: order,
    );
  }

  /// Parse a pre-rendered trace line like `read_file({"path": "x"})` that the
  /// persisted transcript stores on role=tool messages.
  static ActivityStep fromTraceLine(String line,
      {required String id, int order = 0}) {
    final m = RegExp(r'^([a-zA-Z0-9_\-\.]+)\((\{.*\})?\)?\s*$').firstMatch(line);
    if (m != null) {
      final name = m.group(1)!;
      var detail = '';
      final argsJson = m.group(2);
      if (argsJson != null) {
        detail = summarizeArgs(_tryDecode(argsJson));
      }
      return ActivityStep(
          id: id, name: name, detail: detail, status: 'done', order: order);
    }
    return ActivityStep(
        id: id, name: line, status: 'done', order: order);
  }

  static dynamic _tryDecode(String s) {
    try {
      // Dart RegExp captured something like {"path": "."} — parse leniently.
      return jsonDecode(s);
    } catch (_) {
      return null;
    }
  }

  static String summarizeArgs(dynamic args) {
    if (args is! Map) return '';
    for (final k in [
      'path', 'file_path', 'command', 'query', 'url', 'name', 'pattern', 'prompt'
    ]) {
      final v = args[k];
      if (v is String && v.trim().isNotEmpty) {
        return v.length > 80 ? '${v.substring(0, 77)}…' : v;
      }
    }
    return '';
  }

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
  /// Answer text segments — one per streamed answer segment (a turn can
  /// contain several LLM streams interleaved with tool activity).
  final List<String> segments = [];
  /// Assistant reasoning / thinking stream (optional).
  String reasoning = '';
  bool reasoningStreaming = false;
  bool streaming = false;
  final DateTime createdAt;
  /// Media paths attached to a message (best-effort display).
  List<String> media = [];
  /// Ordered activity steps shown while the assistant works on this turn.
  final List<ActivityStep> activity = [];
  /// Whether this turn ended with an error breadcrumb.
  bool hasError = false;
  /// Cost/effort telemetry stamped on turn_end (llm_calls, prompt_tokens…).
  Map<String, num>? usage;
  int? latencyMs;
  String? turnId;

  ChatMessage({
    required this.id,
    required this.role,
    String text = '',
    this.reasoning = '',
    this.streaming = false,
    DateTime? createdAt,
    List<String>? media,
    List<ActivityStep>? activity,
    this.hasError = false,
    this.usage,
    this.latencyMs,
    this.turnId,
    List<String>? segments,
  })  : createdAt = createdAt ?? DateTime.now() {
    if (segments != null) {
      this.segments.addAll(segments);
    } else if (text.isNotEmpty) {
      this.segments.add(text);
    }
    this.media = media ?? [];
    if (activity != null) this.activity.addAll(activity);
  }

  /// Full answer text across all segments.
  String get text {
    if (segments.isEmpty) return '';
    return segments.join('\n\n');
  }

  set text(String value) {
    segments
      ..clear()
      ..add(value);
  }

  /// The segment currently receiving deltas (last one), creating one if needed.
  String get liveSegment => segments.isEmpty ? '' : segments.last;

  void appendDelta(String chunk) {
    if (segments.isEmpty) {
      segments.add('');
    }
    segments[segments.length - 1] = segments.last + chunk;
  }

  /// Finalize the live segment with authoritative stream text (if given) and
  /// start a fresh segment for any following stream.
  void endSegment([String? finalText]) {
    if (segments.isEmpty) {
      if (finalText != null && finalText.isNotEmpty) segments.add(finalText);
      return;
    }
    if (finalText != null) {
      // stream_end carries the complete buffered text for the stream.
      segments[segments.length - 1] = finalText;
    }
    segments.add('');
  }

  void dropEmptyTrailingSegment() {
    while (segments.isNotEmpty && segments.last.trim().isEmpty) {
      segments.removeLast();
    }
  }

  bool get isEmpty =>
      text.trim().isEmpty &&
      reasoning.trim().isEmpty &&
      activity.isEmpty;

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
/// One parsed `tool_events[]` / persisted trace entry.
///
/// Shared by the live WebSocket parser and the persisted-history parser.
class ThreadHistory {
  final List<ChatMessage> messages;
  final String? activeTurnId;
  final bool hasPendingToolCalls;

  ThreadHistory({
    required this.messages,
    this.activeTurnId,
    this.hasPendingToolCalls = false,
  });

  /// Parse `/api/sessions/{key}/webui-thread` payloads:
  /// `{schemaVersion, sessionKey, messages:[{role, turnPhase, kind, content,
  /// reasoning, toolEvents, traces, media, turnId, turnSeq, ...}],
  /// active_turn_id, has_pending_tool_calls, completed_turn_ids}`.
  static ThreadHistory parse(dynamic payload) {
    final out = <ChatMessage>[];
    if (payload is! Map) return ThreadHistory(messages: out);
    final raw = payload['messages'];
    if (raw is! List) {
      return ThreadHistory(
        messages: out,
        activeTurnId: payload['active_turn_id'] as String?,
        hasPendingToolCalls: payload['has_pending_tool_calls'] == true,
      );
    }

    ChatMessage? curAssistant; // assistant bubble accumulating this turn
    String? curTurnId;
    var stepOrder = 0;

    void flush() {
      if (curAssistant != null && !curAssistant!.isEmpty) {
        curAssistant!.dropEmptyTrailingSegment();
        curAssistant!.streaming = false;
        out.add(curAssistant!);
      }
      curAssistant = null;
      curTurnId = null;
    }

    ChatMessage ensureAssistant(String? turnId, int? seq) {
      if (curAssistant != null && (turnId == null || curTurnId == turnId)) {
        return curAssistant!;
      }
      flush();
      curAssistant = ChatMessage(
        id: 'h-a-${seq ?? out.length}-${DateTime.now().microsecondsSinceEpoch}',
        role: Role.assistant,
        turnId: turnId,
      );
      curTurnId = turnId;
      return curAssistant!;
    }

    for (final item in raw) {
      if (item is! Map) continue;
      final m = Map<String, dynamic>.from(item);
      final role = (m['role'] ?? '').toString();
      final phase = (m['turnPhase'] ?? '').toString();
      final kind = (m['kind'] ?? '').toString();
      final content = (m['content'] ?? '').toString();
      final turnId = m['turnId'] as String?;
      final seq = m['turnSeq'] is num ? (m['turnSeq'] as num).toInt() : null;
      final media = _mediaUrls(m);

      if (role == 'user') {
        flush();
        if (content.trim().isNotEmpty || media.isNotEmpty) {
          out.add(ChatMessage(
            id: 'h-u-${seq ?? out.length}',
            role: Role.user,
            text: content,
            media: media,
            turnId: turnId,
          ));
        }
        continue;
      }

      if (role == 'tool' || (role == 'assistant' && kind == 'trace')) {
        final t = ensureAssistant(turnId, seq);
        // Persisted traces carry pre-rendered lines and/or raw tool events.
        final toolEvents = m['toolEvents'];
        var added = false;
        if (toolEvents is List) {
          for (final te in toolEvents) {
            if (te is! Map) continue;
            final step = ActivityStep.fromToolEvent(
              Map<String, dynamic>.from(te),
              order: stepOrder++,
            );
            if (step != null) {
              t.activity.add(step);
              added = true;
            }
          }
        }
        final traces = m['traces'];
        final lines = <String>[];
        if (traces is List) {
          lines.addAll(traces.whereType<String>());
        } else if (content.trim().isNotEmpty) {
          lines.addAll(content.split('\n').where((l) => l.trim().isNotEmpty));
        }
        if (lines.isNotEmpty) {
          for (final line in lines) {
            t.activity.add(ActivityStep.fromTraceLine(
                line.trim(), id: 't-$stepOrder', order: stepOrder++));
          }
        } else if (!added && kind == 'progress' && content.trim().isEmpty) {
          // empty progress breadcrumb — skip silently
        }
        continue;
      }

      if (role == 'assistant') {
        if (phase == 'reasoning' ||
            (m['reasoning'] is String && (m['reasoning'] as String).isNotEmpty)) {
          final t = ensureAssistant(turnId, seq);
          final r = (m['reasoning'] ?? '') as String;
          if (r.isNotEmpty) t.reasoning = t.reasoning.isEmpty ? r : '${t.reasoning}\n\n$r';
          if (content.isNotEmpty && r.isEmpty && phase == 'reasoning') {
            t.reasoning = t.reasoning.isEmpty ? content : '${t.reasoning}\n\n$content';
          }
          continue;
        }
        // answer / complete / everything else that carries text
        final t = ensureAssistant(turnId, seq);
        if (content.trim().isNotEmpty) {
          t.segments.add(content);
        }
        if (media.isNotEmpty) {
          t.media = [...t.media, ...media.where((u) => !t.media.contains(u))];
        }
        final usage = m['usage'] ?? m['turnUsage'];
        if (usage is Map && t.usage == null) {
          t.usage = usage.map((k, v) => MapEntry(k.toString(), v is num ? v : num.tryParse('$v') ?? 0));
        }
        final lat = m['latencyMs'] ?? m['latency_ms'];
        if (lat is num && t.latencyMs == null) t.latencyMs = lat.toInt();
        continue;
      }
    }
    flush();

    return ThreadHistory(
      messages: out,
      activeTurnId: payload['active_turn_id'] as String?,
      hasPendingToolCalls: payload['has_pending_tool_calls'] == true,
    );
  }

  static List<String> _mediaUrls(Map m) {
    final out = <String>[];
    for (final field in ['media', 'media_urls', 'mediaAttachments']) {
      final v = m[field];
      if (v is! List) continue;
      for (final e in v) {
        if (e is String && e.isNotEmpty) {
          out.add(e);
        } else if (e is Map) {
          final u = e['url'] ?? e['full'] ?? e['data_url'];
          if (u is String && u.isNotEmpty) out.add(u);
        }
      }
    }
    return out;
  }
}

