import 'dart:convert';
import 'dart:io';

import 'package:path_provider/path_provider.dart';

import '../models.dart';

/// On-device persistence for chat transcripts.
///
/// Why this exists: the gateway only replays `goal_state` + `goal_status`
/// when a client re-attaches — it does NOT replay the accumulated answer
/// deltas of an in-flight turn. Without a local copy, closing the app
/// mid-turn (or opening a chat while the history REST call fails) showed an
/// empty conversation. The cache renders instantly on open and is merged
/// with the authoritative server history when it arrives.
///
/// The codec is pure (no IO) so it is unit-testable; [ChatCache] only adds
/// the file plumbing on top.
class ChatCache {
  ChatCache({Directory? root}) : _rootOverride = root;

  final Directory? _rootOverride;
  Directory? _root;

  static const int maxMessagesPerChat = 400;
  static const int maxCachedChats = 60;

  Future<Directory> _dir() async {
    final cached = _root;
    if (cached != null) return cached;
    final base = _rootOverride ?? await getApplicationSupportDirectory();
    final dir = Directory('${base.path}/chat_cache');
    if (!await dir.exists()) {
      await dir.create(recursive: true);
    }
    _root = dir;
    return dir;
  }

  static String _fileName(String chatId) {
    final safe = chatId.replaceAll(RegExp(r'[^A-Za-z0-9_.-]'), '_');
    return '$safe.json';
  }

  Future<void> save(String chatId, List<ChatMessage> messages) async {
    if (chatId.isEmpty) return;
    try {
      final dir = await _dir();
      final file = File('${dir.path}/${_fileName(chatId)}');
      await file.writeAsString(encode(messages), flush: true);
      await _trim(dir);
    } catch (_) {
      // Cache is best-effort: never let it break the chat.
    }
  }

  Future<List<ChatMessage>> load(String chatId) async {
    if (chatId.isEmpty) return const [];
    try {
      final dir = await _dir();
      final file = File('${dir.path}/${_fileName(chatId)}');
      if (!await file.exists()) return const [];
      return decode(await file.readAsString());
    } catch (_) {
      return const [];
    }
  }

  Future<void> delete(String chatId) async {
    if (chatId.isEmpty) return;
    try {
      final dir = await _dir();
      final file = File('${dir.path}/${_fileName(chatId)}');
      if (await file.exists()) await file.delete();
    } catch (_) {}
  }

  /// Wipe every cached transcript (used on sign-out so one account's chat
  /// content never survives into another account on the same device).
  Future<void> clearAll() async {
    try {
      final dir = await _dir();
      await for (final entity in dir.list()) {
        if (entity is File && entity.path.endsWith('.json')) {
          try {
            await entity.delete();
          } catch (_) {}
        }
      }
    } catch (_) {}
  }

  /// Keep the cache bounded: drop the oldest chat files past [maxCachedChats].
  Future<void> _trim(Directory dir) async {
    try {
      final files = await dir
          .list()
          .where((e) => e is File && e.path.endsWith('.json'))
          .cast<File>()
          .toList();
      if (files.length <= maxCachedChats) return;
      final stats = <MapEntry<File, DateTime>>[];
      for (final f in files) {
        try {
          stats.add(MapEntry(f, (await f.stat()).modified));
        } catch (_) {}
      }
      stats.sort((a, b) => b.value.compareTo(a.value));
      for (final e in stats.skip(maxCachedChats)) {
        try {
          await e.key.delete();
        } catch (_) {}
      }
    } catch (_) {}
  }

  // ---- codec ------------------------------------------------------------

  /// Serialize messages to a compact JSON string.
  static String encode(List<ChatMessage> messages) {
    final keep = messages.length > maxMessagesPerChat
        ? messages.sublist(messages.length - maxMessagesPerChat)
        : messages;
    return jsonEncode({
      'v': 1,
      'messages': keep.map(_messageToJson).toList(),
    });
  }

  /// Parse a cached payload. Tolerates anything malformed by returning [].
  static List<ChatMessage> decode(String raw) {
    try {
      final root = jsonDecode(raw);
      if (root is! Map) return const [];
      final list = root['messages'];
      if (list is! List) return const [];
      final out = <ChatMessage>[];
      for (final item in list) {
        if (item is! Map) continue;
        final m = _messageFromJson(Map<String, dynamic>.from(item));
        if (m != null) out.add(m);
      }
      return out;
    } catch (_) {
      return const [];
    }
  }

  static Map<String, dynamic> _messageToJson(ChatMessage m) => {
        'id': m.id,
        'role': m.role == Role.user ? 'user' : 'assistant',
        'segments': m.segments,
        'reasoning': m.reasoning,
        'media': m.media,
        'hasError': m.hasError,
        'createdAt': m.createdAt.millisecondsSinceEpoch,
        if (m.turnId != null) 'turnId': m.turnId,
        if (m.usage != null) 'usage': m.usage,
        if (m.latencyMs != null) 'latencyMs': m.latencyMs,
        'activity': m.activity.map(_activityToJson).toList(),
      };

  static Map<String, dynamic> _activityToJson(ActivityStep s) => {
        'id': s.id,
        'name': s.name,
        'detail': s.detail,
        'status': s.status,
        'order': s.order,
        'startedAt': s.startedAt.millisecondsSinceEpoch,
      };

  static ChatMessage? _messageFromJson(Map<String, dynamic> j) {
    final id = j['id'];
    if (id is! String || id.isEmpty) return null;
    final role = j['role'] == 'user' ? Role.user : Role.assistant;
    final segments = <String>[];
    final rawSegments = j['segments'];
    if (rawSegments is List) {
      segments.addAll(rawSegments.whereType<String>());
    }
    final media = <String>[];
    final rawMedia = j['media'];
    if (rawMedia is List) media.addAll(rawMedia.whereType<String>());
    final activity = <ActivityStep>[];
    final rawActivity = j['activity'];
    if (rawActivity is List) {
      for (final a in rawActivity) {
        if (a is! Map) continue;
        final step = _activityFromJson(Map<String, dynamic>.from(a));
        if (step != null) activity.add(step);
      }
    }
    Map<String, num>? usage;
    final rawUsage = j['usage'];
    if (rawUsage is Map) {
      usage = {};
      for (final e in rawUsage.entries) {
        final n = e.value is num ? e.value as num : num.tryParse('${e.value}');
        if (n != null) usage['${e.key}'] = n;
      }
      if (usage.isEmpty) usage = null;
    }
    final createdAt = j['createdAt'] is num
        ? DateTime.fromMillisecondsSinceEpoch((j['createdAt'] as num).toInt())
        : DateTime.now();
    return ChatMessage(
      id: id,
      role: role,
      segments: segments,
      reasoning: (j['reasoning'] ?? '') as String,
      streaming: false,
      createdAt: createdAt,
      media: media,
      activity: activity,
      hasError: j['hasError'] == true,
      usage: usage,
      latencyMs: j['latencyMs'] is num ? (j['latencyMs'] as num).toInt() : null,
      turnId: j['turnId'] as String?,
    );
  }

  static ActivityStep? _activityFromJson(Map<String, dynamic> j) {
    final name = j['name'];
    if (name is! String || name.isEmpty) return null;
    return ActivityStep(
      id: (j['id'] ?? '') as String,
      name: name,
      detail: (j['detail'] ?? '') as String,
      status: (j['status'] ?? 'done') as String,
      order: j['order'] is num ? (j['order'] as num).toInt() : 0,
      startedAt: j['startedAt'] is num
          ? DateTime.fromMillisecondsSinceEpoch((j['startedAt'] as num).toInt())
          : null,
    );
  }
}

/// Merge authoritative server history with the local cache.
///
/// Rules:
///  * Server messages win whenever they carry the same turn identity.
///  * If the server has an assistant bubble for a cached turn but its text is
///    empty (the turn was still streaming when the transcript was written),
///    the cached text/reasoning/activity is folded in so the answer is never
///    lost.
///  * Cached messages the server does not know about are appended, which
///    covers a turn that completed while the app was closed and whose
///    transcript write lagged behind the local copy.
///  * User echoes are de-duplicated by text, and assistant answers are
///    de-duplicated by their normalised text: an answer the server already
///    holds is never appended a second time. Without that, a finished task's
///    results were re-added to the transcript on every reopen (the "results
///    keep firing" bug) because the live bubble carries no server turn id
///    until the turn ends.
List<ChatMessage> mergeThreadHistory({
  required List<ChatMessage> server,
  required List<ChatMessage> cached,
}) {
  if (cached.isEmpty) return List<ChatMessage>.from(server);
  if (server.isEmpty) return List<ChatMessage>.from(cached);

  final merged = List<ChatMessage>.from(server);
  final serverUserTexts = <String>{
    for (final m in merged)
      if (m.role == Role.user && m.text.trim().isNotEmpty) m.text.trim(),
  };
  final serverAssistantByTurn = <String, ChatMessage>{};
  final serverAssistantTexts = <String>{};
  for (final m in merged) {
    if (m.role != Role.assistant) continue;
    if ((m.turnId ?? '').isNotEmpty) {
      serverAssistantByTurn[m.turnId!] = m;
    }
    final t = m.text.trim();
    if (t.isNotEmpty) serverAssistantTexts.add(normalizeAssistantText(t));
  }

  for (final c in cached) {
    if (c.role == Role.user) {
      final t = c.text.trim();
      if (t.isEmpty || serverUserTexts.contains(t)) continue;
      merged.add(c);
      serverUserTexts.add(t);
      continue;
    }
    final turnId = c.turnId ?? '';
    final existing = turnId.isEmpty ? null : serverAssistantByTurn[turnId];
    if (existing != null) {
      // Fill gaps only — never overwrite authoritative server text.
      if (existing.text.trim().isEmpty && c.text.trim().isNotEmpty) {
        existing.segments
          ..clear()
          ..addAll(c.segments.where((s) => s.trim().isNotEmpty));
      }
      if (existing.reasoning.trim().isEmpty && c.reasoning.trim().isNotEmpty) {
        existing.reasoning = c.reasoning;
      }
      if (existing.activity.isEmpty && c.activity.isNotEmpty) {
        existing.activity.addAll(c.activity);
      }
      for (final u in c.media) {
        if (!existing.media.contains(u)) existing.media.add(u);
      }
      continue;
    }
    // Unknown to the server: only keep it when it actually carries content
    // the server does not already have. Text-level dedupe also protects
    // answers cached before (or without) a turn id.
    if (c.text.trim().isEmpty && c.reasoning.trim().isEmpty) continue;
    if (c.text.trim().isNotEmpty &&
        serverAssistantTexts.contains(normalizeAssistantText(c.text.trim()))) {
      continue;
    }
    merged.add(c);
  }
  return merged;
}

/// Collapse whitespace so two renderings of the same answer compare equal.
String normalizeAssistantText(String text) =>
    text.trim().replaceAll(RegExp(r'\s+'), ' ');