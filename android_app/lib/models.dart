/// Data models for the PowerX native client.
library;

enum Role { user, assistant }

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

  ChatMessage({
    required this.id,
    required this.role,
    this.text = '',
    this.reasoning = '',
    this.streaming = false,
    DateTime? createdAt,
    List<String>? media,
  })  : createdAt = createdAt ?? DateTime.now(),
        media = media ?? [];

  bool get isEmpty => text.trim().isEmpty && reasoning.trim().isEmpty;
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

/// A persisted turn from /api/sessions/{key}/webui-thread
class ThreadTurn {
  final String role; // "user" | "assistant"
  final String content;
  final String? reasoning;
  final List<String> media;

  ThreadTurn({
    required this.role,
    required this.content,
    this.reasoning,
    this.media = const [],
  });

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
