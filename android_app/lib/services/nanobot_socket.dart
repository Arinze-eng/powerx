import 'dart:async';
import 'dart:convert';

import 'package:web_socket_channel/web_socket_channel.dart';
import 'package:web_socket_channel/status.dart' as ws_status;

import '../config.dart';

/// Streaming callback for a single chat turn.
typedef ChatDelta = void Function(String text);
typedef ChatDone = void Function(String fullText, List<String> media);
typedef ChatError = void Function(String detail);

class _Turn {
  final ChatDelta onDelta;
  final ChatDone onDone;
  final ChatError onError;
  final StringBuffer buffer = StringBuffer();
  final StringBuffer reasoningBuffer = StringBuffer();
  String? activeStreamId;
  _Turn(this.onDelta, this.onDone, this.onError);
}

/// Native WebSocket client for the nanobot gateway chat protocol.
///
/// Implements the happy path of the documented wire protocol:
///   connect -> {"event":"ready", chat_id}
///   send    -> {"type":"new_chat"}            recv {"event":"attached", chat_id}
///   send    -> {"type":"message","chat_id":..,"content":..,"webui":true}
///   recv    -> {"event":"delta",...} x N       {"event":"stream_end",...}
///         or -> {"event":"message", "text":..}
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
      case 'message':
        final t = chatId == null ? null : _turns[chatId];
        if (t != null) {
          final text = (ev['text'] ?? '') as String;
          final media = <String>[];
          if (ev['media'] is List) {
            for (final m in (ev['media'] as List)) {
              if (m is String) media.add(m);
            }
          }
          _turns.remove(chatId);
          t.onDone(text.isNotEmpty ? text : t.buffer.toString(), media);
        }
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

  /// Provision a fresh chat and resolve with its chat_id.
  Future<String> newChat({Duration timeout = const Duration(seconds: 8)}) async {
    final completer = Completer<String>();
    // The server replies `attached` with a NEW chat_id we don't know yet.
    // Track it by completing the first attached whose id isn't already known.
    _pendingNewChat['__any__'] = completer;
    _send({'type': 'new_chat'});
    // Fallback: capture any newly attached chat id.
    final timer = Timer(timeout, () {
      if (!completer.isCompleted) {
        completer.completeError(TimeoutException('new_chat timed out'));
      }
    });
    completer.future.whenComplete(timer.cancel);
    return completer.future;
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
    required ChatDelta onDelta,
    required ChatDone onDone,
    required ChatError onError,
  }) {
    _turns[chatId] = _Turn(onDelta, onDone, onError);
    _send({
      'type': 'message',
      'chat_id': chatId,
      'content': content,
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
