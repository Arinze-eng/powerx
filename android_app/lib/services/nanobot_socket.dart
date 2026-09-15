import 'dart:async';
import 'dart:convert';
import 'dart:math';

import 'package:web_socket_channel/io.dart';
import 'package:web_socket_channel/web_socket_channel.dart';
import 'package:web_socket_channel/status.dart' as ws_status;

import '../config.dart';
import '../models.dart';

/// Live view callbacks for one chat's turn, mirroring the WebUI's
/// useNanobotStream semantics. All callbacks run on the UI isolate; the
/// listener mutates its ChatMessage and repaints.
class ChatView {
  /// Answer-stream text chunk appended to the live segment.
  final void Function(String chunk) onDelta;
  /// A tool/progress activity step arrived.
  final void Function(List<ActivityStep> steps) onActivity;
  /// Reasoning ("thinking") chunk / end.
  final void Function(String chunk) onReasoningDelta;
  final void Function() onReasoningEnd;
  /// An answer stream closed. [finalText] (when set) is the authoritative
  /// buffered text for that stream — replace the live segment with it.
  final void Function(String? finalText) onStreamEnd;
  /// The whole turn finished (turn_end OR goal_status idle OR final message).
  final void Function(TurnSummary summary) onTurnEnd;
  /// Server-reported error for this chat's turn.
  final void Function(String detail) onError;
  /// A projected user message (echo / replay) for this chat.
  final void Function(String text, String? turnId) onUserMessage;
  /// Raw `message` event without kind — authoritative final assistant text
  /// (also used by /stop acknowledgements when no turn is active).
  final void Function(String text, List<String> media) onFinalMessage;

  const ChatView({
    required this.onDelta,
    required this.onActivity,
    required this.onReasoningDelta,
    required this.onReasoningEnd,
    required this.onStreamEnd,
    required this.onTurnEnd,
    required this.onError,
    required this.onUserMessage,
    required this.onFinalMessage,
  });
}

class TurnSummary {
  final Map<String, num>? usage;
  final int? latencyMs;
  final List<String> media;
  const TurnSummary({this.usage, this.latencyMs, this.media = const []});
}

/// Token pair used to (re)establish a connection. [ws] is the gateway WS
/// token; re-acquired from bootstrap right before every (re)connect because
/// gateway tokens expire after only a few minutes.
class WsToken {
  final String token;
  final String wsPath;
  const WsToken(this.token, this.wsPath);
}

/// Native WebSocket client for the nanobot gateway chat protocol with
/// automatic reconnect + chat re-attach (mirrors webui/src/lib/nanobot-client).
class NanobotSocket {
  /// Supplies a FRESH gateway token (app re-bootstraps; Supabase token may
  /// itself refresh). Called on initial connect and before every reconnect.
  final Future<WsToken> Function() tokenProvider;

  /// Base ws(s):// URL. Production uses [PowerXConfig.wsOrigin]; tests can
  /// point at a local fake gateway.
  final String wsBase;

  WebSocketChannel? _channel;
  StreamSubscription? _sub;
  bool _closedByUser = false;
  bool _connecting = false;
  int _reconnectAttempts = 0;
  Timer? _reconnectTimer;

  /// Views (live UI listeners) per attached chat.
  final Map<String, ChatView> _views = {};
  /// Chat ids attached since connect — re-attached automatically after a
  /// reconnect so backgrounded turns resume streaming into the same view.
  final Set<String> _attachedChats = {};
  /// Chats with an active turn on the client side.
  final Set<String> _activeTurns = {};
  final Set<String> _finalizedTurns = {};
  final Map<String, Completer<String>> _pendingNewChat = {};
  final Random _rng = Random();

  /// Connectivity notifications for the app layer.
  void Function(bool connected)? onConnectionChanged;
  /// Live status of a chat's background turn ("running" | "idle").
  void Function(String chatId, String status)? onGoalStatus;
  /// Session list should refresh.
  void Function()? onSessionsChanged;
  /// Model name changed server-side.
  void Function(String model)? onModelUpdated;

  NanobotSocket({required this.tokenProvider, String? wsBase})
      : wsBase = wsBase ?? PowerXConfig.wsOrigin;

  bool get isConnected => _channel != null;

  // ---- connection lifecycle --------------------------------------------

  Future<void> connect() async {
    if (_connecting) return;
    _connecting = true;
    try {
      final tk = await tokenProvider();
      var path = tk.wsPath;
      if (!path.startsWith('/')) path = '/$path';
      final uri = Uri.parse(
          '$wsBase$path?token=${Uri.encodeComponent(tk.token)}');
      final ch = IOWebSocketChannel.connect(
        uri,
        pingInterval: const Duration(seconds: 20),
      );
      await ch.ready;
      _channel = ch;
      _reconnectAttempts = 0;
      _connecting = false;
      _sub = ch.stream.listen(
        _onData,
        onDone: _onDisconnect,
        onError: (_) => _onDisconnect(),
        cancelOnError: true,
      );
      onConnectionChanged?.call(true);
    } catch (e) {
      _connecting = false;
      // Surface as an immediate error to any waiting newChat completer.
      for (final c in _pendingNewChat.values) {
        if (!c.isCompleted) c.completeError(e);
      }
      _pendingNewChat.clear();
      rethrow;
    }
  }

  void _onDisconnect() {
    if (_closedByUser) return;
    _sub?.cancel();
    _sub = null;
    _channel = null;
    onConnectionChanged?.call(false);
    _scheduleReconnect();
  }

  void _scheduleReconnect() {
    if (_closedByUser || _reconnectTimer != null) return;
    _reconnectAttempts++;
    final base = min(30, pow(2, min(_reconnectAttempts, 5)).toInt());
    final delay = Duration(seconds: base + _rng.nextInt(2));
    _reconnectTimer = Timer(delay, () async {
      _reconnectTimer = null;
      if (_closedByUser) return;
      try {
        await connect();
        // Re-attach every chat we had subscribed to. The server replays
        // goal_status + pending turn events for runs still in flight.
        for (final cid in _attachedChats.toList()) {
          _send({'type': 'attach', 'chat_id': cid});
        }
      } catch (_) {
        _scheduleReconnect();
      }
    });
  }

  void _send(Map<String, dynamic> frame) {
    _channel?.sink.add(jsonEncode(frame));
  }

  // ---- inbound events ----------------------------------------------------

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
      case 'ready':
        final cid = ev['chat_id'] as String?;
        if (cid != null) _pendingNewChat.remove('__any__')?.complete(cid);
        break;
      case 'attached':
        if (chatId != null) {
          final wasAttached = _attachedChats.contains(chatId);
          _attachedChats.add(chatId);
          _pendingNewChat.remove(chatId)?.complete(chatId);
          if (!wasAttached) {
            _pendingNewChat.remove('__any__')?.complete(chatId);
          }
        }
        break;
      case 'delta':
        _view(chatId)?.onDelta((ev['text'] ?? '') as String);
        break;
      case 'reasoning_delta':
        _view(chatId)?.onReasoningDelta((ev['text'] ?? '') as String);
        break;
      case 'reasoning_end':
        _view(chatId)?.onReasoningEnd();
        break;
      case 'stream_end':
        // NOT terminal: a turn contains many answer streams. The event may
        // carry the authoritative buffered `text` for the stream that ended.
        final v = _view(chatId);
        v?.onStreamEnd(ev['text'] is String ? ev['text'] as String : null);
        break;
      case 'message':
        _handleMessageEvent(ev, chatId);
        break;
      case 'file_edit':
        _handleFileEdit(ev, chatId);
        break;
      case 'turn_end':
        _endTurn(chatId,
            usage: _numMap(ev['usage']),
            latencyMs: ev['latency_ms'] is num
                ? (ev['latency_ms'] as num).toInt()
                : null);
        break;
      case 'goal_status':
        final status = ev['status'] as String?;
        if (chatId != null && status != null) {
          if (status == 'running') {
            // A turn started server-side (ours or a backgrounded one):
            // mark active so the eventual turn_end/idle closes it exactly once.
            _activeTurns.add(chatId);
          }
          onGoalStatus?.call(chatId, status);
          if (status == 'idle') {
            // Terminal for the current turn. Cancellation/direct runs may
            // have no turn_end, so idle is still terminal.
            _endTurn(chatId, usage: null, latencyMs: null);
          }
        }
        break;
      case 'session_updated':
        onSessionsChanged?.call();
        break;
      case 'user_message':
        final v = _view(chatId);
        if (v != null) {
          v.onUserMessage((ev['text'] ?? '') as String, ev['turn_id'] as String?);
          if (ev['starts_turn'] == true || ev['active_turn_id'] != null) {
            _activeTurns.add(chatId!);
          }
        }
        break;
      case 'turn_model_updated':
      case 'runtime_model_updated':
        final model = ev['model'] as String?;
        if (model != null && model.isNotEmpty) onModelUpdated?.call(model);
        break;
      case 'error':
        final detail = (ev['detail'] ?? 'error') as String;
        final v = _view(chatId);
        if (v != null) v.onError(detail);
        break;
    }
  }

  void _handleMessageEvent(Map<String, dynamic> ev, String? chatId) {
    final kind = ev['kind'] as String?;
    final v = _view(chatId);
    if (v == null) return;
    final text = (ev['text'] ?? '') as String;

    if (kind == 'tool_hint' || kind == 'progress') {
      final steps = <ActivityStep>[];
      final toolEvents = ev['tool_events'];
      var order = 0;
      if (toolEvents is List) {
        // Merge same-call_id events within one breadcrumb (start then end):
        // the final phase wins so the UI shows a single settled row.
        final merged = <String, ActivityStep>{};
        for (final te in toolEvents) {
          if (te is! Map) continue;
          final s = ActivityStep.fromToolEvent(
            Map<String, dynamic>.from(te),
            order: order,
          );
          if (s == null) continue;
          if (!merged.containsKey(s.id)) {
            order++;
            merged[s.id] = s;
          } else if (s.status != 'running') {
            merged[s.id] = s;
          }
        }
        steps.addAll(merged.values);
        order += steps.length;
      }
      if (steps.isEmpty && text.trim().isNotEmpty) {
        steps.add(ActivityStep.fromTraceLine(text.trim(),
            id: 'h-${DateTime.now().microsecondsSinceEpoch}',
            order: order));
      }
      if (steps.isNotEmpty) v.onActivity(steps);
      return;
    }

    if (kind == 'reasoning') {
      // Legacy complete-reasoning breadcrumb.
      if (text.trim().isNotEmpty) v.onReasoningDelta(text);
      v.onReasoningEnd();
      return;
    }

    // Final assistant reply (no kind): authoritative text + media.
    final media = <String>[];
    final murls = ev['media_urls'];
    if (murls is List) {
      for (final m in murls) {
        if (m is Map && m['url'] is String) media.add(m['url'] as String);
      }
    }
    if (media.isEmpty && ev['media'] is List) {
      for (final m in (ev['media'] as List)) {
        if (m is String && m.startsWith('http')) media.add(m);
      }
    }
    final usage = _numMap(ev['usage']);
    final lat = ev['latency_ms'] is num ? (ev['latency_ms'] as num).toInt() : null;
    v.onFinalMessage(text, media);
    _endTurn(chatId, usage: usage, latencyMs: lat);
  }

  void _handleFileEdit(Map<String, dynamic> ev, String? chatId) {
    final v = _view(chatId);
    if (v == null) return;
    final edits = ev['edits'];
    if (edits is! List) return;
    final steps = <ActivityStep>[];
    var order = 0;
    for (final e in edits) {
      if (e is! Map) continue;
      final path = (e['path'] ?? e['file'] ?? '').toString();
      if (path.isEmpty) continue;
      final status = (e['status'] ?? e['phase'] ?? '').toString();
      steps.add(ActivityStep(
        id: 'fe-$path',
        name: 'edit',
        detail: path.split('/').last,
        status: status == 'editing' || status == 'start' ? 'running' : 'done',
        order: order++,
      ));
    }
    if (steps.isNotEmpty) v.onActivity(steps);
  }

  void _endTurn(String? chatId,
      {required Map<String, num>? usage, required int? latencyMs}) {
    if (chatId == null) return;
    if (!_activeTurns.remove(chatId)) {
      // turn_end arriving twice / idle without a live turn: ignore.
      return;
    }
    _finalizedTurns.add(chatId);
    _view(chatId)?.onTurnEnd(TurnSummary(usage: usage, latencyMs: latencyMs));
  }

  ChatView? _view(String? chatId) => chatId == null ? null : _views[chatId];

  static Map<String, num>? _numMap(dynamic v) {
    if (v is! Map) return null;
    final out = <String, num>{};
    for (final e in v.entries) {
      final n = e.value is num ? e.value as num : num.tryParse('${e.value}');
      if (n != null && n >= 0) out['${e.key}'] = n;
    }
    return out.isEmpty ? null : out;
  }

  // ---- outbound ----------------------------------------------------------

  /// Provision a fresh persistent chat (server `new_chat`) and resolve with
  /// its chat_id.
  Future<String> newChat({Duration timeout = const Duration(seconds: 10)}) async {
    final completer = Completer<String>();
    _pendingNewChat['__any__'] = completer;
    _send({'type': 'new_chat'});
    final timer = Timer(timeout, () {
      final c = _pendingNewChat.remove('__any__');
      if (c != null && !c.isCompleted) {
        c.completeError(TimeoutException('new_chat timed out'));
      }
    });
    return completer.future.whenComplete(timer.cancel);
  }

  /// Subscribe to an existing chat. Safe to call repeatedly.
  Future<String> attach(String chatId,
      {Duration timeout = const Duration(seconds: 10)}) async {
    if (_attachedChats.contains(chatId)) return chatId;
    final completer = Completer<String>();
    _pendingNewChat[chatId] = completer;
    _send({'type': 'attach', 'chat_id': chatId});
    final timer = Timer(timeout, () {
      final c = _pendingNewChat.remove(chatId);
      if (c != null && !c.isCompleted) {
        c.completeError(TimeoutException('attach timed out'));
      }
    });
    return completer.future.whenComplete(timer.cancel);
  }

  /// Register the live UI listener for [chatId]. The previous listener (if
  /// any) is replaced.
  void listen(String chatId, ChatView view) {
    _views[chatId] = view;
    _finalizedTurns.remove(chatId);
  }

  void unlisten(String chatId) => _views.remove(chatId);

  /// Start a turn: register the active flag, then send the message frame.
  void sendMessage(
    String chatId,
    String content, {
    List<Map<String, dynamic>>? media,
  }) {
    _activeTurns.add(chatId);
    _finalizedTurns.remove(chatId);
    _send({
      'type': 'message',
      'chat_id': chatId,
      'content': content,
      if (media != null && media.isNotEmpty) 'media': media,
      'webui': true,
    });
  }

  /// Cancel the running task on [chatId] (server-side /stop semantics).
  void stopTask(String chatId) => sendMessage(chatId, '/stop');

  /// Whether a turn for [chatId] was active and finalized since [listen].
  bool sawTurnEnd(String chatId) => _finalizedTurns.contains(chatId);

  void close() {
    _closedByUser = true;
    _reconnectTimer?.cancel();
    _reconnectTimer = null;
    _sub?.cancel();
    _channel?.sink.close(ws_status.normalClosure);
    _channel = null;
    _views.clear();
    _attachedChats.clear();
    _activeTurns.clear();
  }
}
