import 'dart:convert';

import 'package:flutter_secure_storage/flutter_secure_storage.dart';

/// Minimal key/value surface so the durable send queue can be unit-tested
/// without a platform channel (flutter_secure_storage requires one).
abstract class KeyValueStore {
  Future<String?> read(String key);
  Future<void> write(String key, String value);
  Future<void> delete(String key);
}

/// Production implementation backed by the encrypted platform store — the same
/// place the session tokens live, so nothing new needs to be provisioned.
class SecureKeyValueStore implements KeyValueStore {
  SecureKeyValueStore([FlutterSecureStorage? storage])
      : _storage = storage ?? const FlutterSecureStorage();

  final FlutterSecureStorage _storage;

  @override
  Future<String?> read(String key) => _storage.read(key: key);

  @override
  Future<void> write(String key, String value) =>
      _storage.write(key: key, value: value);

  @override
  Future<void> delete(String key) => _storage.delete(key: key);
}

/// One user message that has been accepted by the UI but not yet confirmed as
/// written to the gateway socket.
///
/// Why this exists: `sendMessage` used to optimistically mark the turn active
/// and then hand the frame to a socket that may already be dead. The frame
/// landed in the socket's *in-memory* outbox, so an app process kill (which is
/// exactly what "user closes the app" does on Android) discarded the task
/// silently — the user came back to a task that never ran, or to a half
/// rendered answer with no steps. Persisting the pending frame means the task
/// is (re)sent as soon as the app can reach the gateway again, whether that is
/// on the next reconnect or after a cold start.
class PendingSend {
  final String id;
  final String chatId;
  final String content;
  final List<Map<String, dynamic>>? media;
  final String? turnId;
  final int createdAtMs;

  const PendingSend({
    required this.id,
    required this.chatId,
    required this.content,
    this.media,
    this.turnId,
    required this.createdAtMs,
  });

  Map<String, dynamic> toJson() => {
        'id': id,
        'chat_id': chatId,
        'content': content,
        if (media != null && media!.isNotEmpty) 'media': media,
        if (turnId != null) 'turn_id': turnId,
        'created_at_ms': createdAtMs,
      };

  static PendingSend? fromJson(Map<String, dynamic> j) {
    final id = j['id'];
    final chatId = j['chat_id'];
    if (id is! String || id.isEmpty) return null;
    if (chatId is! String || chatId.isEmpty) return null;
    final rawMedia = j['media'];
    List<Map<String, dynamic>>? media;
    if (rawMedia is List) {
      media = rawMedia
          .whereType<Map>()
          .map((m) => Map<String, dynamic>.from(m))
          .toList();
    }
    return PendingSend(
      id: id,
      chatId: chatId,
      content: (j['content'] ?? '') as String,
      media: (media == null || media.isEmpty) ? null : media,
      turnId: j['turn_id'] as String?,
      createdAtMs: j['created_at_ms'] is num
          ? (j['created_at_ms'] as num).toInt()
          : DateTime.now().millisecondsSinceEpoch,
    );
  }

  /// The exact `message` frame the gateway expects.
  Map<String, dynamic> toWireFrame() => {
        'type': 'message',
        'chat_id': chatId,
        'content': content,
        if (media != null && media!.isNotEmpty) 'media': media,
        if (turnId != null) 'turn_id': turnId,
        'webui': true,
      };
}

/// Durable queue of unconfirmed user messages.
///
/// Bounded and FIFO: on overflow the OLDEST entry is dropped, because a stale
/// task from ten minutes ago is less valuable than the message the user just
/// typed. Sends older than [maxAge] are discarded on load — replaying a very
/// old frame would start a surprise task.
class PendingSendQueue {
  PendingSendQueue(this._store, {this.key = 'pending_sends_v1'});

  final KeyValueStore _store;
  final String key;

  /// Beyond this, a queued frame is considered stale rather than pending.
  static const Duration maxAge = Duration(hours: 6);
  static const int maxEntries = 20;

  /// Pure codec (no IO) so the persistence behaviour is unit-testable.
  static String encode(List<PendingSend> sends) =>
      jsonEncode({'v': 1, 'sends': sends.map((s) => s.toJson()).toList()});

  static List<PendingSend> decode(String raw) {
    try {
      final root = jsonDecode(raw);
      if (root is! Map) return const [];
      final list = root['sends'];
      if (list is! List) return const [];
      final out = <PendingSend>[];
      for (final item in list) {
        if (item is! Map) continue;
        final send = PendingSend.fromJson(Map<String, dynamic>.from(item));
        if (send != null) out.add(send);
      }
      return out;
    } catch (_) {
      return const [];
    }
  }

  /// Drop entries that are too old or malformed. Pure, so it is testable.
  static List<PendingSend> prune(
    List<PendingSend> sends, {
    DateTime? now,
    Duration maxAge = maxAge,
    int maxEntries = maxEntries,
  }) {
    final ref = now ?? DateTime.now();
    final kept = <PendingSend>[];
    for (final s in sends) {
      final age = ref.difference(
        DateTime.fromMillisecondsSinceEpoch(s.createdAtMs),
      );
      if (age > maxAge) continue;
      if (s.content.trim().isEmpty && (s.media == null || s.media!.isEmpty)) {
        continue;
      }
      kept.add(s);
    }
    if (kept.length > maxEntries) {
      return kept.sublist(kept.length - maxEntries);
    }
    return kept;
  }

  Future<List<PendingSend>> load() async {
    final raw = await _store.read(key);
    if (raw == null || raw.isEmpty) return const [];
    final pruned = prune(decode(raw));
    return pruned;
  }

  Future<void> save(List<PendingSend> sends) async {
    if (sends.isEmpty) {
      await _store.delete(key);
      return;
    }
    await _store.write(key, encode(prune(sends)));
  }

  Future<void> clear() => _store.delete(key);
}