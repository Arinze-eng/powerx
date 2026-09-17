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
/// The goal is one and only one visible copy of every message and every
/// activity step, no matter how many times the app is closed and reopened
/// mid-task.
///
/// Rules, applied in order:
///  1. Same turn identity → the server row is authoritative and the cached
///     copy is *folded into it* (never appended beside it).
///  2. No usable turn id, but the server already holds an identical answer →
///     the same fold happens, matched by normalised text. This is the case
///     that produced the duplicate chat: the live bubble only learns its turn
///     id on `turn_end`, so an app killed mid-turn carried no id and was
///     appended as a second answer.
///  3. The trailing assistant bubble of an interrupted turn (empty turn id) is
///     folded into the server's trailing turn, so a partial answer that the
///     cache captured cannot appear twice.
///  4. Anything else unknown to the server is kept only when it carries real
///     content — streamed text, reasoning, activity or media — so a task's
///     results are never silently dropped.
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
    final tid = m.turnId ?? '';
    if (tid.isNotEmpty && !serverAssistantByTurn.containsKey(tid)) {
      serverAssistantByTurn[tid] = m;
    }
    final t = normalizeAssistantText(m.text);
    if (t.isNotEmpty) serverAssistantTexts.add(t);
  }

  /// Last assistant bubble in the merged (server) transcript, if it is the
  /// tail — the only bubble an interrupted turn can legitimately extend.
  ChatMessage? trailingServerAssistant() {
    if (merged.isEmpty) return null;
    final last = merged.last;
    return last.role == Role.assistant ? last : null;
  }

  var lastCachedAssistantIndex = -1;
  for (var i = cached.length - 1; i >= 0; i--) {
    if (cached[i].role == Role.assistant) {
      lastCachedAssistantIndex = i;
      break;
    }
  }

  for (var i = 0; i < cached.length; i++) {
    final c = cached[i];
    if (c.role == Role.user) {
      final t = c.text.trim();
      if (t.isEmpty || serverUserTexts.contains(t)) continue;
      merged.add(c);
      serverUserTexts.add(t);
      continue;
    }

    final turnId = c.turnId ?? '';

    // 1) Known turn: fold into the authoritative server bubble for that turn.
    final byTurn = turnId.isEmpty ? null : serverAssistantByTurn[turnId];
    if (byTurn != null) {
      byTurn.mergeFrom(c);
      continue;
    }

    final cachedText = normalizeAssistantText(c.text);

    // 2) The server already holds this exact answer — fold, never append.
    if (cachedText.isNotEmpty && serverAssistantTexts.contains(cachedText)) {
      ChatMessage? twin;
      for (final m in merged) {
        if (m.role == Role.assistant &&
            normalizeAssistantText(m.text) == cachedText) {
          twin = m;
          break;
        }
      }
      if (twin != null) {
        twin.mergeFrom(c);
        continue;
      }
    }

    // 3) The tail bubble of a turn that never reported its completion: fold it
    //    into the server's trailing turn, which is where that work landed.
    if (i == lastCachedAssistantIndex && turnId.isEmpty) {
      final tail = trailingServerAssistant();
      if (tail != null) {
        tail.mergeFrom(c);
        continue;
      }
    }

    // 4) Genuinely unknown to the server: keep it when it has content.
    if (c.text.trim().isEmpty &&
        c.reasoning.trim().isEmpty &&
        c.activity.isEmpty &&
        c.media.isEmpty) {
      continue;
    }
    merged.add(c);
    if (cachedText.isNotEmpty) serverAssistantTexts.add(cachedText);
    if (turnId.isNotEmpty) serverAssistantByTurn[turnId] = c;
  }
  return merged;
}

/// Re-link [live] — the bubble currently receiving streamed deltas — back into
/// a freshly merged transcript, and return the list the UI should render.
///
/// Why this is needed: [mergeThreadHistory] builds its result from SERVER-owned
/// [ChatMessage] instances, so the object the UI holds as its live streaming
/// target is not necessarily among them any more. The screen replaces its
/// message list with the merged result on every resync (turn start, every
/// reconnect, and every 15 s while a long turn runs), which orphaned that
/// bubble: inbound `delta` frames kept appending to an object that was no
/// longer rendered. The visible symptom was a long task appearing to be "cut
/// off" — the transcript froze mid-answer while the busy indicator kept
/// spinning and the answer only landed once a terminal frame arrived.
///
/// The streamed bubble is the row of record while a turn runs, so it is kept
/// and the server snapshot is folded INTO it (via [ChatMessage.mergeFrom],
/// which only fills gaps and therefore never clobbers streamed text).
///
/// Returns the merged list; the live bubble is placed at the end when the
/// server has not persisted its row yet, so it stays the visual tail.
List<ChatMessage> relinkLiveTurn({
  required List<ChatMessage> merged,
  required ChatMessage? live,
  String? activeTurnId,
}) {
  if (live == null) return merged;

  final liveTurnId = (live.turnId ?? '').trim();
  final tid =
      liveTurnId.isNotEmpty ? liveTurnId : (activeTurnId ?? '').trim();

  // Drop any copy of this bubble already in the merged list, otherwise folding
  // it back in could leave two rows for one turn.
  merged.remove(live);

  var targetIndex = -1;
  if (tid.isNotEmpty) {
    for (var i = 0; i < merged.length; i++) {
      final m = merged[i];
      if (m.role == Role.assistant && (m.turnId ?? '').trim() == tid) {
        targetIndex = i;
        break;
      }
    }
  }

  if (targetIndex < 0) {
    // The server holds no persisted row for this turn yet — normal while it is
    // mid-stream. The streamed bubble IS that row.
    merged.add(live);
    return merged;
  }

  live.mergeFrom(merged[targetIndex]);
  merged[targetIndex] = live;
  return merged;
}