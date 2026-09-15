import 'dart:async';
import 'dart:convert';

import 'package:web_socket_channel/web_socket_channel.dart';
import 'package:web_socket_channel/status.dart' as ws_status;

import '../config.dart';
import '../models.dart';

/// Streaming callbacks for a single chat turn.
typedef ChatDelta = void Function(String text);
typedef ChatDone = void Function(String fullText, List<String> media);
typedef ChatError = void Function(String detail);
/// One intermediate activity step emitted while the agent works.
typedef ChatActivity = void Function(ActivityStep step);
/// Fired when the server reports the turn finished (turn_end / goal idle).
typedef ChatTurnEnd = void Function();

class _Turn {
  final ChatDelta onDelta;
  final ChatDone onDone;
  final ChatError onError;
  final ChatActivity? onActivity;
  final StringBuffer buffer = StringBuffer();
  final StringBuffer reasoningBuffer = StringBuffer();
  String? activeStreamId;
  int stepOrder = 0;
  _Turn(this.onDelta, this.onDone, this.onError, this.onActivity);
}

/// Native WebSocket client for the nanobot gateway chat protocol.
///
/// Implements the documented wire protocol:
///   connect -> {"event":"ready", chat_id}
///   send    -> {"type":"new_chat"}            recv {"event":"attached", chat_id}
///   send    -> {"type":"attach","chat_id":..} recv {"event":"attached", ...}
///   send    -> {"type":"message","chat_id":..,"content":..,"media":[..],"webui":true}
///   recv    -> {"event":"delta",...} x N       {"event":"stream_end",...}
///         or -> {"event":"message", "text":..}
///         +  {"event":"message","kind":"tool_hint"/"progress","tool_events":[..]}
class NanobotSocket {
  final String token;
  final String wsPath;

  WebSocketChannel? _channel;
  StreamSubscription? _sub;
  final Map<String, _Turn> _turns = {}; // chat_id -> active turn
  final Map<String, Completer<String>> _pendingNewChat = {};
  final Set<String> _attached = {};
  bool _open = false;

  /// Called when the socket drops unexpectedly so the app can re-bootstrap.
  void Function()? onDisconnected;

  /// Live status of a chat's background turn ("running" | "idle").
  void Function(String chatId, String status)? onGoalStatus;

  NanobotSocket({required this.token, required this.wsPath});

  Uri get _url {
    var path = wsPath;
    if (!path.startsWith('/')) path = '/$path';
    return Uri.parse('${PowerXConfig.wsOrigin}$path?token=${Uri.encodeComponent(token)}');
  }

  Future<void> connect() async {
    final ch = WebSocketChannel.connect(_url);
    await ch.ready;
    _channel = ch;
    _open = true;
    _sub = ch.stream.listen(
      _onData,
      onDone: () {
        _open = false;
        onDisconnected?.call();
      },
      onError: (_) {
        _open = false;
      },
      cancelOnError: true,
    );
  }

  void _send(Map<String, dynamic> frame) {
    _channel?.sink.add(jsonEncode(frame));
  }

  void _onData(dynamic raw) {
    late final Map<String, dynamic> ev;
    try {
      ev = jsonDecode(raw as String) as Map<String, dynamic>;
    } catch (_) {
      return;
    }
    final event = ev['event'] as String?;
    final chatId = ev['chat_id'] as String?;

    switch (event) {
      case 'attached':
        if (chatId != null) {
          _maybeResolveNewChat(chatId);
          _attached.add(chatId);
          _pendingNewChat.remove(chatId)?.complete(chatId);
        }
        break;
      case 'delta':
        final t = chatId == null ? null : _turns[chatId];
        if (t != null) {
          final sid = ev['stream_id'];
          if (sid is String) t.activeStreamId = sid;
          final text = (ev['text'] ?? '') as String;
          t.buffer.write(text);
          t.onDelta(text);
        }
        break;
      case 'reasoning_delta':
        final t = chatId == null ? null : _turns[chatId];
        if (t != null) t.reasoningBuffer.write((ev['text'] ?? '') as String);
        break;
      case 'stream_end':
        final t = chatId == null ? null : _turns[chatId];
        if (t != null) {
          final full = t.buffer.toString();
          _turns.remove(chatId);
          t.onDone(full, const []);
        }
        break;
      case 'goal_status':
        if (chatId != null && ev['status'] is String) {
          onGoalStatus?.call(chatId, ev['status'] as String);
        }
        break;
      case 'turn_end':
        // The canonical end of a turn. If a stream was still open, close it.
        if (chatId != null) {
          final t = _turns.remove(chatId);
          if (t != null) {
            t.onDone(t.buffer.toString(), const []);
          }
        }
        break;
      case 'message':
        _handleMessageEvent(ev, chatId);
        break;
      case 'error':
        final detail = (ev['detail'] ?? 'error') as String;
        if (chatId != null && _turns.containsKey(chatId)) {
          final t = _turns.remove(chatId)!;
          t.onError(detail);
        }
        break;
    }
  }

  /// A `message` event is either the final assistant reply OR an intermediate
  /// breadcrumb (`kind: tool_hint` / `kind: progress`) carrying tool activity.
  void _handleMessageEvent(Map<String, dynamic> ev, String? chatId) {
    final kind = ev['kind'] as String?;
    final isActivity = kind == 'tool_hint' || kind == 'progress';
    final t = chatId == null ? null : _turns[chatId];

    if (isActivity) {
      if (t == null || t.onActivity == null) return;
      final hint = (ev['text'] ?? '') as String;
      final toolEvents = ev['tool_events'];
      if (toolEvents is List && toolEvents.isNotEmpty) {
        for (final te in toolEvents) {
          if (te is! Map) continue;
          _emitToolEvent(t, chatId!, Map<String, dynamic>.from(te), hint);
        }
      } else if (hint.trim().isNotEmpty) {
        t.onActivity!(ActivityStep(
          id: 'h-${DateTime.now().microsecondsSinceEpoch}',
          name: hint.trim(),
          status: 'done',
          order: t.stepOrder++,
        ));
      }
      return;
    }

    // Final assistant reply.
    if (t != null) {
      final text = (ev['text'] ?? '') as String;
      final media = <String>[];
      final murls = ev['media_urls'];
      if (murls is List) {
        for (final m in murls) {
          if (m is Map && m['url'] is String) media.add(m['url'] as String);
        }
      }
      if (media.isEmpty && ev['media'] is List) {
        for (final m in (ev['media'] as List)) {
          if (m is String) media.add(m);
        }
      }
      _turns.remove(chatId);
      t.onDone(text.isNotEmpty ? text : t.buffer.toString(), media);
    }
  }

  void _emitToolEvent(_Turn t, String chatId, Map<String, dynamic> te, String hint) {
    final step = parseToolEvent(te, order: t.stepOrder);
    if (step == null) return;
    t.stepOrder++;
    t.onActivity!(step);
  }

  /// Pure mapping of one wire `tool_events[]` entry to an [ActivityStep].
  /// Returns null when the event carries no usable tool name.
  static ActivityStep? parseToolEvent(Map<String, dynamic> te, {int order = 0}) {
    final phase = (te['phase'] ?? '').toString();
    final name = (te['name'] ?? '').toString();
    final callId = (te['call_id'] ?? '').toString();
    if (name.isEmpty) return null;
    final detail = summarizeArgs(te['arguments']);
    final status = phase == 'start'
        ? 'running'
        : phase == 'error'
            ? 'error'
            : 'done';
    return ActivityStep(
      id: callId.isNotEmpty ? callId : '$name-$order',
      name: name,
      detail: detail,
      status: status,
      order: order,
    );
  }

  static String summarizeArgs(dynamic args) {
    if (args is! Map) return '';
    // Prefer a human-friendly key.
    for (final k in ['path', 'file_path', 'command', 'query', 'url', 'name', 'pattern']) {
      final v = args[k];
      if (v is String && v.trim().isNotEmpty) {
        return v.length > 80 ? '${v.substring(0, 77)}…' : v;
      }
    }
    return '';
  }

  /// Provision a fresh chat and resolve with its chat_id.
  Future<String> newChat({Duration timeout = const Duration(seconds: 8)}) async {
    final completer = Completer<String>();
    _pendingNewChat['__any__'] = completer;
    _send({'type': 'new_chat'});
    final timer = Timer(timeout, () {
      if (!completer.isCompleted) {
        completer.completeError(TimeoutException('new_chat timed out'));
      }
    });
    completer.future.whenComplete(timer.cancel);
    return completer.future;
  }

  /// Re-attach to an existing chat (used for resuming a backgrounded turn).
  Future<String> attach(String chatId, {Duration timeout = const Duration(seconds: 8)}) async {
    if (_attached.contains(chatId)) return chatId;
    final completer = Completer<String>();
    _pendingNewChat[chatId] = completer;
    _send({'type': 'attach', 'chat_id': chatId});
    final timer = Timer(timeout, () {
      if (!completer.isCompleted) {
        _pendingNewChat.remove(chatId);
        completer.completeError(TimeoutException('attach timed out'));
      }
    });
    completer.future.whenComplete(timer.cancel);
    return completer.future;
  }

  /// Register a passive observer for [chatId] so events from a turn that was
  /// started before we connected (e.g. resumed after closing the app) still
  /// render live. Callbacks fire just like an explicit [sendMessage].
  void observe(
    String chatId, {
    required ChatDelta onDelta,
    required ChatDone onDone,
    required ChatError onError,
    ChatActivity? onActivity,
  }) {
    if (_turns.containsKey(chatId)) return; // already owned by a real send
    _turns[chatId] = _Turn(onDelta, onDone, onError, onActivity);
  }

  /// Stop observing a chat without disturbing a real send.
  void unobserve(String chatId) {
    _turns.remove(chatId);
  }

  void _maybeResolveNewChat(String chatId) {
    if (!_attached.contains(chatId)) {
      _pendingNewChat.remove('__any__')?.complete(chatId);
    }
  }

  /// Send a user message on [chatId] and stream the assistant reply.
  void sendMessage(
    String chatId,
    String content, {
    List<Map<String, dynamic>>? media,
    required ChatDelta onDelta,
    required ChatDone onDone,
    required ChatError onError,
    ChatActivity? onActivity,
  }) {
    _turns[chatId] = _Turn(onDelta, onDone, onError, onActivity);
    _send({
      'type': 'message',
      'chat_id': chatId,
      'content': content,
      if (media != null && media.isNotEmpty) 'media': media,
      'webui': true,
    });
  }

  void close() {
    _sub?.cancel();
    _channel?.sink.close(ws_status.normalClosure);
    _open = false;
    _turns.clear();
    _attached.clear();
  }

  bool get isOpen => _open;
}
