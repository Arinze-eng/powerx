/// Data models for the PowerX native client.
library;

import 'dart:convert';

enum Role { user, assistant }

/// Collapse whitespace so two renderings of the same answer compare equal.
///
/// Lives here (not in the cache layer) because both the cache merge and the
/// transcript dedupe logic need it, and the cache layer already depends on
/// these models — putting it there would create an import cycle.
String normalizeAssistantText(String text) =>
    text.trim().replaceAll(RegExp(r'\s+'), ' ');

/// A single in-progress "step" the agent emits while working on a turn —
/// mirrors the WebUI's activity timeline (tool hints + tool_events).
class ActivityStep {
  String id;
  String name; // tool name or hint text
  String detail; // short argument summary
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

  /// Render one `tool_events[]` entry the way the WebUI's
  /// `formatToolCallTrace` does, e.g. `read_file({"path": "."})`.
  ///
  /// Used to recognise a persisted `traces[]` line that merely restates a tool
  /// event, so the same call is never rendered as two activity rows.
  static String? traceLineFromToolEvent(Map event) {
    final name = (event['name'] ?? event['tool'] ?? '').toString();
    if (name.isEmpty) return null;
    final args = event['arguments'] ?? event['args'];
    if (args is String && args.trim().isNotEmpty) return '$name($args)';
    if (args is Map) {
      final encoded = jsonEncode(args);
      return encoded == '{}' ? '$name()' : '$name($encoded)';
    }
    if (args is List) {
      final encoded = jsonEncode(args);
      return encoded == '[]' ? '$name()' : '$name($encoded)';
    }
    return '$name()';
  }

  /// Normalise a trace line so two renderings of the same call compare equal —
  /// the Dart twin of the WebUI's `canonicalToolTrace`.
  static String canonicalTrace(String line) {
    final trimmed = line.trim();
    final match = RegExp(r'^([a-zA-Z0-9_.\-]+)\((.*)\)$').firstMatch(trimmed);
    if (match == null) return trimmed;
    final name = match.group(1)!;
    final args = match.group(2)!.trim();
    if (args.isEmpty) return '$name()';
    try {
      return '$name(${jsonEncode(jsonDecode(args))})';
    } catch (_) {
      return trimmed;
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

  /// Stable identity of *what this step did*, independent of where it came
  /// from.
  ///
  /// This is the fix for the duplicated activity list. Three sources describe
  /// the same tool call with three different `id`s:
  ///   * live socket `tool_events[]`  → the real tool `call_id`
  ///   * persisted transcript replay  → a generated `trace-3` style id
  ///   * the on-disk chat cache       → whatever id was live when it was saved
  /// Matching on `id` alone therefore rendered every step two or three times.
  /// Tool name + argument summary is the only identity shared by all three.
  String get toolKey {
    final n = name.trim().toLowerCase();
    var d = detail.trim();
    if (d.endsWith('…')) d = d.substring(0, d.length - 1).trim();
    return '$n\u0000$d';
  }

  /// Prefix used for activity ids synthesised by the persisted-transcript
  /// replay. Kept in one place because [ActivityStep.hasStableId] relies on
  /// recognising it — a mismatch between this prefix and the check is exactly
  /// what let the same tool row render twice.
  static const String traceIdPrefix = 'trace-';

  /// Whether [id] is a real gateway tool `call_id` rather than an id
  /// synthesised by a replay / hint / file-edit fallback path.
  bool get hasStableId {
    if (id.isEmpty) return false;
    return !id.startsWith(traceIdPrefix) &&
        !id.startsWith('t-') &&
        !id.startsWith('h-') &&
        !id.startsWith('fe-');
  }

  /// Whether [other] describes the same underlying tool call as this step.
  ///
  /// Matching order:
  ///  1. equal ids always match;
  ///  2. two *different* real `call_id`s are two different calls — a retried
  ///     command genuinely ran twice and must stay two rows;
  ///  3. otherwise (at least one side is a synthetic replay id) match on tool
  ///     identity plus argument summary.
  bool matchesIdentity(ActivityStep other) {
    if (id.isNotEmpty && other.id.isNotEmpty) {
      if (id == other.id) return true;
      if (hasStableId && other.hasStableId) return false;
    }
    if (toolKey == other.toolKey) return true;
    // The persisted transcript stores a truncated argument summary, so a
    // replayed row can carry a prefix of the live detail. Treat that as the
    // same call rather than adding a second row.
    final n = name.trim().toLowerCase();
    if (n != other.name.trim().toLowerCase()) return false;
    final a = detail.trim();
    final b = other.detail.trim();
    if (a.isEmpty || b.isEmpty) return false;
    final shorter = a.length <= b.length ? a : b;
    final longer = a.length <= b.length ? b : a;
    if (shorter.length < 8) return false;
    final stem = shorter.endsWith('…')
        ? shorter.substring(0, shorter.length - 1)
        : shorter;
    return stem.length >= 8 && longer.startsWith(stem);
  }

  static int _statusRank(String s) =>
      s == 'error' ? 3 : (s == 'done' ? 2 : 1);

  /// Fold a second observation of this same step into the row already on
  /// screen: the most advanced status wins (a late `running` replay must never
  /// reopen a finished row) and missing text is filled in, never overwritten.
  void mergeLive(ActivityStep other) {
    if (_statusRank(other.status) > _statusRank(status)) {
      status = other.status;
    }
    if (name.trim().isEmpty) name = other.name;
    if (detail.trim().isEmpty) detail = other.detail;
    // Adopt a real tool call id over a synthetic one so subsequent live frames
    // for this call merge by id as well.
    if (!hasStableId && other.hasStableId) {
      id = other.id;
    }
  }
}

/// Merge [incoming] activity steps into [existing] without ever producing a
/// duplicate row, and without dropping genuine repeats.
///
/// Existing rows keep their position (the timeline must not reshuffle while a
/// turn streams); a replayed copy of a row merges into the row it matches. The
/// `claimed` set means two *identical* commands that really did run twice stay
/// two rows — each incoming copy consumes a distinct existing row.
List<ActivityStep> mergeActivitySteps(
  List<ActivityStep> existing,
  List<ActivityStep> incoming,
) {
  if (incoming.isEmpty) return existing;
  final result = List<ActivityStep>.from(existing);
  final claimed = <int>{};
  for (final step in incoming) {
    var match = -1;
    for (var i = 0; i < result.length; i++) {
      if (claimed.contains(i)) continue;
      if (result[i].matchesIdentity(step)) {
        match = i;
        break;
      }
    }
    if (match >= 0) {
      claimed.add(match);
      result[match].mergeLive(step);
    } else {
      claimed.add(result.length);
      result.add(step);
    }
  }
  return result;
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
  ///
  /// An empty/null [finalText] leaves the streamed text untouched — the
  /// gateway sometimes closes a stream with no buffered text, and clobbering
  /// the segment with `''` erased the answer on screen.
  void endSegment([String? finalText]) {
    if (segments.isEmpty) {
      if (finalText != null && finalText.trim().isNotEmpty) {
        segments.add(finalText);
      }
      return;
    }
    if (finalText != null && finalText.trim().isNotEmpty) {
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

  /// Whether this bubble already renders the *same answer text* as [other].
  ///
  /// Used to recognise a server replay / cache copy of a bubble we are already
  /// showing, so reopening a finished task never appends its results twice.
  bool rendersSameAnswer(ChatMessage other) {
    final a = normalizeAssistantText(text);
    final b = normalizeAssistantText(other.text);
    return a.isNotEmpty && a == b;
  }

  /// Adopt the server's canonical answer text for the stream that just closed.
  ///
  /// The previous guard (`text.length >= t.text.length`) silently DROPPED a
  /// correct-but-shorter final text — that is why results stopped appearing
  /// after the last fix. `stream_end`/final-message text is authoritative by
  /// protocol, so it is taken whenever it is non-empty.
  void absorbFinalText(String? authoritative) {
    if (authoritative == null || authoritative.trim().isEmpty) return;
    if (segments.isEmpty) {
      segments.add(authoritative);
      return;
    }
    segments[segments.length - 1] = authoritative;
  }

  /// Fold a second copy of this same turn (a server replay or a cache copy)
  /// into this bubble without duplicating any visible content.
  ///
  /// Rules, in order of authority:
  ///   * answer text  — the server's wins when both are present;
  ///   * reasoning    — kept, concatenated only when genuinely different;
  ///   * activity     — merged through [mergeActivitySteps] (never duplicated);
  ///   * media/usage/latency/turnId — union, server value wins.
  void mergeFrom(ChatMessage other) {
    final mine = text.trim();
    final theirs = other.text.trim();
    if (mine.isEmpty && theirs.isNotEmpty) {
      segments
        ..clear()
        ..addAll(other.segments.where((s) => s.trim().isNotEmpty));
    }
    final myReasoning = reasoning.trim();
    final theirReasoning = other.reasoning.trim();
    if (myReasoning.isEmpty && theirReasoning.isNotEmpty) {
      reasoning = other.reasoning;
    } else if (theirReasoning.isNotEmpty &&
        normalizeAssistantText(other.reasoning) !=
            normalizeAssistantText(reasoning) &&
        !normalizeAssistantText(reasoning).contains(
            normalizeAssistantText(other.reasoning))) {
      reasoning = '$reasoning\n\n${other.reasoning}';
    }
    final merged = mergeActivitySteps(activity, other.activity);
    activity
      ..clear()
      ..addAll(merged);
    for (final m in other.media) {
      if (!media.contains(m)) media.add(m);
    }
    if (other.usage != null) usage = {...?usage, ...other.usage!};
    latencyMs ??= other.latencyMs;
    if ((turnId ?? '').isEmpty && (other.turnId ?? '').isNotEmpty) {
      turnId = other.turnId;
    }
    if (other.hasError) hasError = true;
  }

  /// Attachments that can be rendered inline (http(s) urls only).
  List<String> get viewableMedia =>
      media.where((m) => m.startsWith('http')).toList();

  /// Distinct file paths this turn wrote/edited (derived from activity
  /// steps), so the UI can offer them as downloadable artifacts while the
  /// turn streams and after it completes.
  List<String> get artifactPaths {
    final out = <String>[];
    for (final s in activity) {
      if (s.iconKey != 'write') continue;
      final p = s.detail.trim();
      if (p.isEmpty || p.endsWith('…')) continue; // truncated arg, not a path
      if (!out.contains(p)) out.add(p);
    }
    return out;
  }
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

    // The persisted transcript is authoritative in `turnSeq` order — the WebUI
    // sorts every row by it before rendering (orderMessagesByTurnSeq). Rows
    // arrive mostly sorted already, but a reasoning row can be stamped after an
    // activity row it actually precedes. Sorting is only safe when EVERY row
    // carries a finite `turnSeq`; otherwise the array order is all we have.
    final rows = raw
        .whereType<Map>()
        .map((e) => Map<String, dynamic>.from(e))
        .toList();
    if (rows.isNotEmpty && rows.every((r) => r['turnSeq'] is num)) {
      final indexed = <MapEntry<int, Map<String, dynamic>>>[
        for (var i = 0; i < rows.length; i++) MapEntry(i, rows[i]),
      ];
      indexed.sort((a, b) {
        final bySeq = (a.value['turnSeq'] as num)
            .toInt()
            .compareTo((b.value['turnSeq'] as num).toInt());
        return bySeq != 0 ? bySeq : a.key - b.key;
      });
      rows
        ..clear()
        ..addAll(indexed.map((e) => e.value));
    }

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

    for (final m in rows) {
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
        final renderedFromEvents = <String>{};
        if (toolEvents is List) {
          for (final te in toolEvents) {
            if (te is! Map) continue;
            final event = Map<String, dynamic>.from(te);
            final line = ActivityStep.traceLineFromToolEvent(event);
            if (line != null) {
              renderedFromEvents.add(ActivityStep.canonicalTrace(line));
            }
            final step = ActivityStep.fromToolEvent(event, order: stepOrder++);
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
        // A persisted `traces[]` line that only restates a tool event must not
        // become a second row — the WebUI's mergeToolProgressTraceLines drops
        // traces already covered by tool events for exactly this reason.
        final seenTraces = <String>{};
        var traceAdded = false;
        for (final line in lines) {
          final key = ActivityStep.canonicalTrace(line);
          if (renderedFromEvents.contains(key) || !seenTraces.add(key)) continue;
          t.activity.add(ActivityStep.fromTraceLine(line.trim(),
              id: '${ActivityStep.traceIdPrefix}$stepOrder',
              order: stepOrder++));
          traceAdded = true;
        }
        if (!traceAdded &&
            !added &&
            kind == 'progress' &&
            content.trim().isEmpty) {
          // empty progress breadcrumb — skip silently
        }
        continue;
      }

      if (role == 'assistant') {
        // `turnPhase` is authoritative. This guard used to treat ANY row with a
        // non-empty `reasoning` as reasoning-only and skip it, but the server
        // also stamps the reasoning tail onto the ANSWER row — a real persisted
        // row is turnPhase "answer", content "LONG-OK-1", reasoning "Now reply
        // exactly LONG-OK-1.". The answer text was silently dropped, which is
        // why a task that had already finished showed no result when the app
        // was reopened.
        //
        // The WebUI splits such a row into two units (assistantHasInlineReasoning
        // -> reasoningOnlyMessageFromAnswer + stripInlineReasoning in
        // lib/activity-timeline.ts). The native bubble keeps both, in the same
        // order, on one card.
        final reasonText =
            m['reasoning'] is String ? m['reasoning'] as String : '';
        final hasAnswerText = content.trim().isNotEmpty;
        final hasReasoning = reasonText.trim().isNotEmpty;
        final isAnswerPhase = phase == 'answer' ||
            phase == 'complete' ||
            phase == 'completed' ||
            phase == 'final';
        final reasoningOnly =
            phase == 'reasoning' || (!isAnswerPhase && !hasAnswerText);
        if (reasoningOnly) {
          final r = hasReasoning ? reasonText : content;
          if (r.trim().isNotEmpty) {
            final t = ensureAssistant(turnId, seq);
            if (!_reasoningAlready(t.reasoning, r)) {
              t.reasoning = t.reasoning.isEmpty ? r : '${t.reasoning}\n\n$r';
            }
          }
          continue;
        }
        // answer / complete / everything else that carries text
        final t = ensureAssistant(turnId, seq);
        if (hasReasoning && !_reasoningAlready(t.reasoning, reasonText)) {
          t.reasoning = t.reasoning.isEmpty
              ? reasonText
              : '${t.reasoning}\n\n$reasonText';
        }
        if (hasAnswerText) {
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

  /// Whether [incoming] reasoning is already present in [existing].
  ///
  /// The transcript can carry the same reasoning twice — once on the reasoning
  /// row and once, as a tail, on the answer row — and folding it in twice would
  /// double the visible thought block.
  static bool _reasoningAlready(String existing, String incoming) {
    final b = normalizeAssistantText(incoming);
    if (b.isEmpty) return true;
    final a = normalizeAssistantText(existing);
    if (a.isEmpty) return false;
    return a == b || a.contains(b);
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

