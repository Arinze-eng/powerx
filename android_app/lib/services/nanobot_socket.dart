import 'dart:async';
import 'dart:convert';
import 'dart:math';

import 'package:web_socket_channel/io.dart';
import 'package:web_socket_channel/web_socket_channel.dart';
import 'package:web_socket_channel/status.dart' as ws_status;

import '../config.dart';
import '../models.dart';
import 'gateway_api.dart';
import 'pending_sends.dart';

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
  /// Usage/credit facts for this chat (attach handshake or turn end).
  final void Function(Map<String, num>? usage) onUsage;

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
    required this.onUsage,
  });
}

class TurnSummary {
  final Map<String, num>? usage;
  final int? latencyMs;
  final List<String> media;
  final String? turnId;
  const TurnSummary({this.usage, this.latencyMs, this.media = const [], this.turnId});
}

/// A completed `webui_response` for a socket mutation.
class _MutationReply {
  final bool ok;
  final Map<String, dynamic> result;
  final int? status;
  final String? message;
  const _MutationReply({
    required this.ok,
    this.result = const {},
    this.status,
    this.message,
  });
}

/// Persistent per-chat window that forwards live events to whichever
/// [ChatView] is currently registered for the chat.
///
/// Why the indirection exists: the socket installs ONE long-lived relay per
/// chat, so inbound events are never dropped while a screen re-registers its
/// listener. Previously `unlisten` removed the view outright, and every frame
/// that arrived in the gap (a reopen / re-attach) was silently discarded —
/// which is what made a running task's steps and stream vanish after closing
/// and reopening the app.
class _ChatViewProxy {
  ChatView? target;

  /// The stable [ChatView] handed to the socket. It reads [target] at call
  /// time, so replacing the listener never changes this object's identity and
  /// no event is lost during the swap.
  ChatView? _relay;

  ChatView get relay => _relay ??= ChatView(
        onDelta: (chunk) => target?.onDelta(chunk),
        onActivity: (steps) => target?.onActivity(steps),
        onReasoningDelta: (chunk) => target?.onReasoningDelta(chunk),
        onReasoningEnd: () => target?.onReasoningEnd(),
        onStreamEnd: (finalText) => target?.onStreamEnd(finalText),
        onTurnEnd: (summary) => target?.onTurnEnd(summary),
        onError: (detail) => target?.onError(detail),
        onUserMessage: (text, turnId) => target?.onUserMessage(text, turnId),
        onFinalMessage: (text, media) => target?.onFinalMessage(text, media),
        onUsage: (usage) => target?.onUsage(usage),
      );
}

/// A durable chat: the chat the user has open, restored after a cold start so
/// a task started before the app was killed keeps streaming into a live view
/// instead of being silently abandoned.
class OpenChat {
  const OpenChat({required this.chatId, required this.sessionKey});
  final String chatId;
  final String? sessionKey;
}

/// Token pair used to (re)establish a connection. [ws] is the gateway WS
/// token; re-acquired from bootstrap right before every (re)connect because
/// gateway tokens expire after only a few minutes.
class WsToken {
  final String token;
  final String wsPath;

  /// Signed-in Supabase access token, sent as ``X-Nanobot-Auth`` on the
  /// handshake. Server-side socket mutations (``session.delete``) authorize
  /// through the connection's Supabase identity; a handshake without it made
  /// every delete answer "session not found" even for the caller's own chat.
  final String? supabaseToken;

  const WsToken(this.token, this.wsPath, {this.supabaseToken});
}

/// Control frames that arrive on the same socket but are NOT chat events.
/// Kept here so the UI layer can filter them out of transcript projection.
const Set<String> kSocketControlEvents = {
  'ready',
  'attached',
  'session_updated',
  'sidebar_state_updated',
  'goal_status',
  'goal_state',
  'message_accepted',
  'turn_model_updated',
  'runtime_model_updated',
  'webui_response',
  'transcription_result',
  'transcription_error',
  'pong',
  'heartbeat',
};

/// Error details the gateway emits for chat-scoped, RECOVERABLE problems.
///
/// These arrive while the turn is still legitimately running, so treating them
/// as terminal cleared the busy state mid-task — the "task gets cut off on a
/// long run" symptom, where the work continued server-side but the UI stopped
/// streaming and only recovered when the user asked again.
///
/// Matched by exact detail (or prefix where the gateway appends context) so a
/// genuine fatal error still ends the turn.
const kRecoverableTurnErrors = <String>{
  'attachment_rejected',
  'message_rejected',
  'invalid temperature chat_id',
  'invalid temporary chat_id',
  'message_deduplicated',
  'queued',
};

bool isRecoverableTurnError(String detail) {
  if (detail.isEmpty) return false;
  if (kRecoverableTurnErrors.contains(detail)) return true;
  // Queued/deduped notices carry the turn id as a suffix.
  return detail.startsWith('queued') || detail.startsWith('duplicate');
}

/// True when [raw] is a protocol/control frame rather than a transcript event.
bool isProtocolFrame(String raw) {
  try {
    final ev = jsonDecode(raw);
    if (ev is! Map) return false;
    final event = ev['event'];
    return event is String && kSocketControlEvents.contains(event);
  } catch (_) {
    return false;
  }
}

/// Native WebSocket client for the nanobot gateway chat protocol with
/// automatic reconnect + chat re-attach (mirrors webui/src/lib/nanobot-client).
///
/// Reliability contract (why this class is defensive):
///  * Outbound frames sent while the socket is down are queued and flushed on
///    (re)connect, so a message typed during a blip is never silently lost.
///  * A turn is only cleared by an authoritative terminal event
///    (`turn_end` / `goal_status idle` / final `message`) — never by a local
///    timer. Long, quiet tool runs therefore never get "cut off".
///  * Connect/reconnect uses unbounded exponential backoff and application
///    pings, so the stream survives screen-off, app background and NAT idle
///    timeouts instead of pausing.
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
  Timer? _pingTimer;
  DateTime _lastInboundAt = DateTime.now();

  /// Frames produced while the socket is not writable, replayed on connect.
  final List<String> _outbox = [];
  static const int _maxOutbox = 40;

  /// Views (live UI listeners) per attached chat, behind a persistent window
  /// so events survive a screen re-registering its listener.
  final Map<String, _ChatViewProxy> _views = {};
  /// Chats the UI wants subscribed. Persists across reconnects so a
  /// backgrounded turn resumes streaming into the same view.
  final Set<String> _wantedChats = {};
  /// Chats CONFIRMED subscribed on the CURRENT connection. Cleared whenever
  /// the socket drops: they are per-connection subscriptions.
  ///
  /// This distinction is the fix for "everything stucks after backgrounding":
  /// previously the confirmed set survived the drop, so the post-reconnect
  /// `attach()` short-circuited and the client never actually re-subscribed —
  /// the server never replayed `goal_status: running`, so the UI sat frozen
  /// with a half-open socket and a dead transcript.
  final Set<String> _attachedChats = {};
  /// Chats with an active turn on the client side.
  final Set<String> _activeTurns = {};
  final Set<String> _finalizedTurns = {};
  /// Last turn that reached a terminal event, per chat. Attach hydration can
  /// replay ``goal_status: running`` for a chat; a replay whose turn id has
  /// already finished here is ignored instead of re-opening the live results
  /// bubble (a finished task "firing" its results again).
  final Map<String, String> _completedTurnIds = {};
  /// Server turn id owning each chat's current run, learned from the frames
  /// that carry one (goal_status running, message, turn_end).
  final Map<String, String> _currentTurnIds = {};
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
  /// Any turn-level event arrived for a chat (used to keep long tasks warm).
  void Function(String chatId)? onTurnActivity;
  /// Server-reported error with no attached view (connection-level).
  void Function(String? chatId, String detail)? onErrorEvent;

  /// Persisted user messages awaiting gateway confirmation. Set by the app
  /// layer (after sign-in) so a task survives an app process kill. Null keeps
  /// the previous in-memory-only behaviour, which tests rely on.
  PendingSendQueue? pendingSends;

  /// The chat the user currently has open, in memory so [restore] can re-open
  /// it without touching storage on the hot path.
  OpenChat? _openChat;

  /// Frames accepted by the UI but not yet confirmed written to the socket.
  final List<PendingSend> _pendingSends = [];
  bool _pendingLoaded = false;
  Timer? _pendingFlushTimer;
  /// Which chat the user is looking at, so a replayed in-flight turn is routed
  /// to the right transcript after a cold start.
  String? _lastActedChatId;

  NanobotSocket({required this.tokenProvider, String? wsBase})
      : wsBase = wsBase ?? PowerXConfig.wsOrigin;

  bool get isConnected => _channel != null;

  /// Whether a turn is currently believed to be running for [chatId].
  bool isTurnActive(String chatId) => _activeTurns.contains(chatId);

  /// The server turn id currently believed to own [chatId]'s run, if known.
  String? activeTurnId(String chatId) => _currentTurnIds[chatId];

  /// Timestamp of the last frame received from the server (any kind).
  DateTime get lastInboundAt => _lastInboundAt;

  // ---- connection lifecycle --------------------------------------------

  Future<void> connect() async {
    if (_connecting) return;
    if (isConnected) return;
    _connecting = true;
    try {
      final tk = await tokenProvider();
      var path = tk.wsPath;
      if (!path.startsWith('/')) path = '/$path';
      final uri = Uri.parse(
          '$wsBase$path?token=${Uri.encodeComponent(tk.token)}&client=apk');
      // Carry the Supabase identity on the handshake. The gateway authorizes
      // socket mutations (session.delete) as the connection's user, and the
      // synthetic mutation request inherits the handshake headers.
      final headers = <String, dynamic>{};
      final supabaseToken = tk.supabaseToken;
      if (supabaseToken != null && supabaseToken.isNotEmpty) {
        headers['X-Nanobot-Auth'] = supabaseToken;
      }
      final ch = IOWebSocketChannel.connect(
        uri,
        headers: headers.isEmpty ? null : headers,
        pingInterval: const Duration(seconds: 20),
        connectTimeout: const Duration(seconds: 20),
      );
      await ch.ready;
      _channel = ch;
      _reconnectAttempts = 0;
      _connecting = false;
      _lastInboundAt = DateTime.now();
      _sub = ch.stream.listen(
        _onData,
        onDone: _onDisconnect,
        onError: (_) => _onDisconnect(),
        cancelOnError: true,
      );
      _startHeartbeat();
      await _flushOutbox();
      // Replay any task the user issued before the app was killed. This runs
      // before onConnectionChanged so the resend is on the wire ahead of the
      // UI's own attach/reconcile work.
      await _flushPendingSends();
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
    _stopHeartbeat();
    _sub?.cancel();
    _sub = null;
    _channel = null;
    // Subscriptions are per-connection: forget what was confirmed so the next
    // attach genuinely re-subscribes and receives the running-turn replay.
    _attachedChats.clear();
    onConnectionChanged?.call(false);
    _scheduleReconnect();
  }

  void _scheduleReconnect() {
    if (_closedByUser || _reconnectTimer != null) return;
    _reconnectAttempts++;
    // Unbounded, capped at 30 s: a long backgrounded turn must always come
    // back rather than give up after a handful of attempts.
    final base = min(30, pow(2, min(_reconnectAttempts, 5)).toInt());
    final delay = Duration(seconds: base + _rng.nextInt(2));
    _reconnectTimer = Timer(delay, () async {
      _reconnectTimer = null;
      if (_closedByUser) return;
      try {
        await connect();
        // Re-subscribe every chat the UI still cares about. The server replays
        // goal_status + the running turn's wall clock for runs in flight, which
        // is what makes a backgrounded task resume instead of staying frozen.
        for (final cid in _wantedChats.toList()) {
          _attachedChats.remove(cid);
          _send({'type': 'attach', 'chat_id': cid});
        }
        // A task typed while offline / before a process kill must still go
        // out now that we are back.
        await _flushPendingSends();
      } catch (_) {
        _scheduleReconnect();
      }
    });
  }

  /// Application-level keepalive.
  ///
  /// IMPORTANT: this must NOT send custom frames. The gateway's WS protocol
  /// only accepts its known envelope types and answers
  /// `error: unknown type: 'ping'` for anything else — which is exactly the
  /// "unknown ping" error users saw. Keepalive is handled at the WS protocol
  /// level by [pingInterval] (control frames are transparent to the server),
  /// plus a local liveness check that forces a clean reconnect on a zombie
  /// socket (see [checkLiveness]).
  void _startHeartbeat() {
    _pingTimer?.cancel();
    _pingTimer = Timer.periodic(const Duration(seconds: 25), (_) {
      if (!isConnected) return;
      // The socket claims to be open but nothing has arrived for a long time:
      // on mobile this is usually a half-open socket after Android suspended
      // the app (no FIN is ever delivered). Force a reconnect so the turn
      // resumes instead of the UI appearing frozen.
      if (DateTime.now().difference(_lastInboundAt) >
          const Duration(seconds: 70)) {
        _onDisconnect();
      }
    });
  }

  /// Verify the socket is genuinely usable, reconnecting if it looks dead.
  ///
  /// Called when the app returns to the foreground. Trusting
  /// [isConnected] alone is unsafe: after a background suspension the TCP
  /// connection is often half-open, so writes silently vanish and the chat
  /// looks "paused" with no error at all.
  Future<bool> checkLiveness({Duration maxIdle = const Duration(seconds: 30)}) async {
    final ch = _channel;
    if (ch == null) return false;
    if (DateTime.now().difference(_lastInboundAt) > maxIdle) {
      // Stale beyond the idle window → tear down and rebuild from scratch.
      _stopHeartbeat();
      await _sub?.cancel();
      _sub = null;
      try {
        ch.sink.close();
      } catch (_) {}
      _channel = null;
      _attachedChats.clear();
      onConnectionChanged?.call(false);
      try {
        await connect();
        for (final cid in _wantedChats.toList()) {
          _send({'type': 'attach', 'chat_id': cid});
        }
        return isConnected;
      } catch (_) {
        _scheduleReconnect();
        return false;
      }
    }
    return true;
  }

  void _stopHeartbeat() {
    _pingTimer?.cancel();
    _pingTimer = null;
  }

  void _send(Map<String, dynamic> frame) {
    final ch = _channel;
    if (ch == null) {
      _queueFrame(jsonEncode(frame));
      return;
    }
    try {
      ch.sink.add(jsonEncode(frame));
    } catch (_) {
      _queueFrame(jsonEncode(frame));
      _onDisconnect();
    }
  }

  void _queueFrame(String raw) {
    // Never drop a user message; drop only stale heartbeats when full.
    _outbox.add(raw);
    while (_outbox.length > _maxOutbox) {
      final idx = _outbox.indexWhere((f) => f.contains('"ping"'));
      _outbox.removeAt(idx >= 0 ? idx : 0);
    }
  }

  Future<void> _flushOutbox() async {
    if (_outbox.isEmpty) return;
    final pending = List<String>.from(_outbox);
    _outbox.clear();
    for (final raw in pending) {
      try {
        _channel?.sink.add(raw);
      } catch (_) {
        _outbox.insert(0, raw);
        break;
      }
    }
  }

  // ---- durable pending sends --------------------------------------------

  /// Load the persisted unconfirmed sends (once per process) and resend them.
  Future<void> _ensurePendingLoaded() async {
    final queue = pendingSends;
    if (queue == null || _pendingLoaded) return;
    _pendingLoaded = true;
    try {
      final loaded = await queue.load();
      if (loaded.isEmpty) return;
      _pendingSends
        ..clear()
        ..addAll(loaded);
      for (final s in _pendingSends) {
        _activeTurns.add(s.chatId);
      }
      await queue.save(_pendingSends);
    } catch (_) {
      // Storage unavailable: fall back to in-memory only.
    }
  }

  /// Re-send every frame the gateway has not confirmed, keeping the rest.
  ///
  /// A frame is dropped from the queue only once the socket has actually
  /// accepted it, so a crash mid-send cannot lose the user's task.
  Future<void> _flushPendingSends() async {
    await _ensurePendingLoaded();
    if (_pendingSends.isEmpty) return;
    final remaining = <PendingSend>[];
    for (final s in _pendingSends) {
      var written = false;
      try {
        _channel?.sink.add(jsonEncode(s.toWireFrame()));
        written = _channel != null;
      } catch (_) {
        written = false;
      }
      if (written) {
        _activeTurns.add(s.chatId);
      } else {
        remaining.add(s);
      }
    }
    _pendingSends
      ..clear()
      ..addAll(remaining);
    await _persistPending();
    if (remaining.isEmpty) {
      // Everything is on the wire: let the UI reconcile against the server.
      onSessionsChanged?.call();
    }
  }

  Future<void> _persistPending() async {
    final queue = pendingSends;
    if (queue == null) return;
    try {
      await queue.save(_pendingSends);
    } catch (_) {}
  }

  /// Record an outbound user message durably BEFORE it reaches the socket.
  ///
  /// Returns immediately; persistence is fire-and-forget so typing never
  /// blocks on disk.
  void _trackPendingSend(
    String chatId,
    String content, {
    List<Map<String, dynamic>>? media,
    String? turnId,
  }) {
    if (pendingSends == null) return;
    final send = PendingSend(
      id: 'ps-${DateTime.now().microsecondsSinceEpoch}',
      chatId: chatId,
      content: content,
      media: media,
      turnId: turnId,
      createdAtMs: DateTime.now().millisecondsSinceEpoch,
    );
    _pendingSends.add(send);
    if (_pendingSends.length > PendingSendQueue.maxEntries) {
      _pendingSends.removeAt(0);
    }
    unawaited(_persistPending());
    // A send that never reaches the wire is retried without waiting for a
    // network change (covers "socket claimed open but is half dead").
    _pendingFlushTimer?.cancel();
    _pendingFlushTimer = Timer(const Duration(seconds: 4), () {
      if (!_closedByUser && _pendingSends.isNotEmpty) {
        unawaited(_flushPendingSends());
      }
    });
  }

  /// Drop a pending send once the server owns the turn. Anything else we may
  /// still be holding for that chat is superseded too — the gateway only runs
  /// one turn per chat.
  void _confirmPendingSend(String chatId, String? turnId) {
    if (_pendingSends.isEmpty) return;
    var changed = false;
    for (var i = _pendingSends.length - 1; i >= 0; i--) {
      final s = _pendingSends[i];
      if (s.chatId != chatId) continue;
      if (turnId != null && s.turnId != null && s.turnId != turnId) continue;
      _pendingSends.removeAt(i);
      changed = true;
    }
    if (changed) unawaited(_persistPending());
  }

  /// Whether a task for [chatId] is still waiting to reach the gateway.
  bool hasPendingSend(String chatId) =>
      _pendingSends.any((s) => s.chatId == chatId);

  // ---- open chat / cold-start recovery -----------------------------------

  /// Remember which chat the user has open and make sure a background turn on
  /// it keeps streaming into a live view.
  void setOpenChat(String chatId, {String? sessionKey}) {
    _openChat = OpenChat(chatId: chatId, sessionKey: sessionKey);
    _lastActedChatId = chatId;
    _wantedChats.add(chatId);
  }

  OpenChat? get openChat => _openChat;

  /// The chat with the most recent gateway turn activity in this process.
  ///
  /// Used as a fallback when the app has to re-open a chat after a restart and
  /// no explicit open-chat marker survived — a task that was streaming is a
  /// better recovery target than an empty new conversation.
  String? get lastActedChatId => _lastActedChatId;

  /// Re-open a chat before/without the UI, and re-subscribe, so an in-flight
  /// task resumes streaming (steps + answer) instead of the user finding a
  /// frozen transcript.
  ///
  /// Attaches are normally driven by the UI; this only sends one when the chat
  /// is not already confirmed subscribed, so it cannot double-subscribe.
  Future<String?> restore({String? chatId}) async {
    final target = chatId ?? _openChat?.chatId ?? _lastActedChatId;
    if (target == null) return null;
    await _ensurePendingLoaded();
    if (!isConnected) {
      try {
        await connect();
      } catch (_) {
        return target;
      }
    }
    if (!_attachedChats.contains(target)) {
      _wantedChats.add(target);
      _send({'type': 'attach', 'chat_id': target});
    }
    await _flushPendingSends();
    return target;
  }

  // ---- inbound events ----------------------------------------------------

  void _onData(dynamic raw) {
    _lastInboundAt = DateTime.now();
    // A malformed or unexpected frame must never propagate: an exception
    // raised inside the stream listener tears down the socket (and, before
    // the global crash fence, the whole Android process). Catch everything,
    // keep the connection, and let the next frame do the work.
    try {
      _handleFrame(raw);
    } catch (_) {
      // Ignore the bad frame; the socket stays usable.
    }
  }

  void _handleFrame(dynamic raw) {
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
          _wantedChats.add(chatId);
          _pendingNewChat.remove(chatId)?.complete(chatId);
          if (!wasAttached) {
            _pendingNewChat.remove('__any__')?.complete(chatId);
          }
          // Handshake model facts: carries the last known usage for this chat,
          // so the footer is correct right after a resume.
          _view(chatId)?.onUsage(_numMap(ev['usage']));
        }
        break;
      case 'delta':
        onTurnActivity?.call(chatId ?? '');
        if (chatId != null) {
          _lastActedChatId = chatId;
          // Streaming began: the gateway is definitely running this turn, so
          // any copy we still hold is stale.
          _confirmPendingSend(chatId, null);
        }
        _view(chatId)?.onDelta((ev['text'] ?? '') as String);
        break;
      case 'reasoning_delta':
        onTurnActivity?.call(chatId ?? '');
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
      case 'message_accepted':
        // Canonical turn ownership for a locally submitted message. The origin
        // client already rendered the optimistic bubble, so we only adopt the
        // run state (never echo the text back as a second user bubble).
        if (chatId != null) {
          _activeTurns.add(chatId);
          _lastActedChatId = chatId;
          final acceptedTurnId = ev['turn_id'] as String?;
          if (acceptedTurnId != null && acceptedTurnId.isNotEmpty) {
            _currentTurnIds[chatId] = acceptedTurnId;
          }
          // The server owns this turn now — stop re-sending it.
          _confirmPendingSend(chatId, acceptedTurnId);
          onTurnActivity?.call(chatId);
        }
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
                : null,
            turnId: ev['turn_id'] as String?);
        break;
      case 'goal_status':
        final status = ev['status'] as String?;
        if (chatId != null && status != null) {
          _lastActedChatId = chatId;
          if (status == 'running') {
            final replayTurnId = ev['turn_id'] as String?;
            if (replayTurnId != null &&
                replayTurnId.isNotEmpty &&
                _completedTurnIds[chatId] == replayTurnId) {
              // Superseded replay of a turn that already ended: ignore it so
              // reopening a finished task cannot re-fire its results.
              break;
            }
            if (replayTurnId != null && replayTurnId.isNotEmpty) {
              _currentTurnIds[chatId] = replayTurnId;
            }
            // A turn started server-side (ours or a backgrounded one):
            // mark active so the eventual turn_end/idle closes it exactly once.
            _activeTurns.add(chatId);
            // The gateway is executing this chat's turn — stop re-sending.
            _confirmPendingSend(chatId, null);
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
        if (chatId != null) _lastActedChatId = chatId;
        // The proxy keeps the window alive even if the screen is between
        // listeners, so this projection is never lost mid-reopen.
        final v = _view(chatId);
        if (v != null) {
          final text = (ev['text'] ?? '') as String;
          final turnId = ev['turn_id'] as String?;
          if (text.isNotEmpty) v.onUserMessage(text, turnId);
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
      case 'webui_response':
        _handleWebuiResponse(ev);
        break;
      case 'transcription_result':
        _handleTranscriptionResult(ev);
        break;
      case 'transcription_error':
        _handleTranscriptionError(ev);
        break;
      case 'error':
        final detail = (ev['detail'] ?? 'error') as String;
        // Not every error frame ends the turn. The gateway emits `error` for
        // recoverable, chat-scoped problems too — a rejected attachment, a
        // deduped/queued message, an invalid temporary id — and those arrive
        // WHILE the turn keeps running. The previous build treated any error
        // as terminal on both sides:
        //   * onErrorError cleared `_remoteRunning`;
        //   * the socket dropped the chat from `_activeTurns`.
        // The visible result was exactly the reported symptom: a long task got
        // "cut off" in the UI mid-run, stopped streaming, and only reappeared
        // when the user asked again. Recoverable details are now surfaced as a
        // breadcrumb on the live turn without touching run state.
        if (isRecoverableTurnError(detail)) {
          // Keep the run alive: these are chat-scoped, recoverable problems
          // that arrive while the turn continues. Treat it as activity so the
          // liveness clock is touched, and surface nothing destructive.
          onTurnActivity?.call(chatId ?? '');
          break;
        }
        final v = _view(chatId);
        if (v != null) {
          v.onError(detail);
        } else {
          onErrorEvent?.call(chatId, detail);
        }
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
      if (steps.isNotEmpty) {
        onTurnActivity?.call(chatId ?? '');
        v.onActivity(steps);
      }
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
    _endTurn(chatId,
        usage: usage,
        latencyMs: lat,
        media: media,
        turnId: ev['turn_id'] as String?);
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
    if (steps.isNotEmpty) {
      onTurnActivity?.call(chatId ?? '');
      v.onActivity(steps);
    }
  }

  void _endTurn(String? chatId,
      {required Map<String, num>? usage,
      required int? latencyMs,
      List<String> media = const [],
      String? turnId}) {
    if (chatId == null) return;
    if (turnId != null && turnId.isNotEmpty) {
      _completedTurnIds[chatId] = turnId;
    }
    _currentTurnIds.remove(chatId);
    if (!_activeTurns.remove(chatId)) {
      // Terminal event for a turn we did not think was active. Two cases:
      // (a) duplicate/late terminal event — already finalized, ignore;
      // (b) the attach replay of goal_status idle for a turn that finished
      // between the history fetch and the subscribe. In (b) the UI armed its
      // busy indicator from that stale history snapshot, so still terminate
      // it exactly once — otherwise the green indicator rolls forever.
      if (_finalizedTurns.contains(chatId)) return;
      _finalizedTurns.add(chatId);
      _view(chatId)?.onTurnEnd(
          TurnSummary(usage: usage, latencyMs: latencyMs, media: media, turnId: turnId));
      return;
    }
    _finalizedTurns.add(chatId);
    _view(chatId)?.onTurnEnd(
        TurnSummary(usage: usage, latencyMs: latencyMs, media: media, turnId: turnId));
  }

  ChatView? _view(String? chatId) =>
      chatId == null ? null : _views[chatId]?.target;

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
  /// its chat_id. Connects first when needed so a cold start can one-shot.
  Future<String> newChat({Duration timeout = const Duration(seconds: 15)}) async {
    if (!isConnected) await connect();
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

  /// Subscribe to an existing chat. Safe to call repeatedly; re-sends the
  /// frame when the server has not acked it on THIS connection (idempotent
  /// server-side). Re-subscribing after a reconnect is mandatory to receive
  /// the running-turn replay.
  Future<String> attach(String chatId,
      {Duration timeout = const Duration(seconds: 15)}) async {
    _wantedChats.add(chatId);
    if (!isConnected) await connect();
    if (_attachedChats.contains(chatId)) return chatId;
    final existing = _pendingNewChat[chatId];
    if (existing != null) return existing.future;
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
  /// any) is replaced inside the chat's persistent window.
  ///
  /// The window itself is never torn down here, so inbound frames keep being
  /// routed to the chat (not dropped) during the moment a screen is between
  /// listeners — the reopen / re-attach gap where a running task's steps and
  /// stream used to disappear.
  void listen(String chatId, ChatView view) {
    final proxy = _views.putIfAbsent(chatId, _ChatViewProxy.new);
    proxy.target = view;
    _finalizedTurns.remove(chatId);
  }

  /// Detach the UI listener but KEEP the chat's window so events for a
  /// backgrounded turn are still processed and the next screen that opens this
  /// chat resumes mid-stream.
  void unlisten(String chatId) {
    final proxy = _views[chatId];
    if (proxy == null) return;
    proxy.target = null;
  }

  /// Forget all client-side state for a chat that was deleted server-side, so
  /// a later re-attach cannot resurrect a stale busy indicator.
  void dropChat(String chatId) {
    _views.remove(chatId);
    _attachedChats.remove(chatId);
    _wantedChats.remove(chatId);
    _activeTurns.remove(chatId);
    _finalizedTurns.remove(chatId);
    _completedTurnIds.remove(chatId);
    _currentTurnIds.remove(chatId);
    _pendingNewChat.remove(chatId);
    if (_openChat?.chatId == chatId) _openChat = null;
    final hadPending =
        _pendingSends.any((s) => s.chatId == chatId);
    _pendingSends.removeWhere((s) => s.chatId == chatId);
    if (hadPending) unawaited(_persistPending());
  }

  /// Mark a turn as started for [chatId] before the server confirms it, so a
  /// terminal event that arrives first is still attributed correctly.
  void markUserTurn(String chatId) {
    _activeTurns.add(chatId);
    _finalizedTurns.remove(chatId);
  }

  /// Start a turn: register the active flag, then send the message frame.
  void sendMessage(
    String chatId,
    String content, {
    List<Map<String, dynamic>>? media,
    String? turnId,
  }) {
    _activeTurns.add(chatId);
    _finalizedTurns.remove(chatId);
    _lastActedChatId = chatId;
    _wantedChats.add(chatId);
    // Persist BEFORE the frame reaches the socket. If the app is killed (or
    // the socket is already half dead) the task is re-sent on the next
    // connect instead of vanishing, and it is cleared the moment the gateway
    // answers `message_accepted` or starts streaming.
    _trackPendingSend(chatId, content, media: media, turnId: turnId);
    _send({
      'type': 'message',
      'chat_id': chatId,
      'content': content,
      if (media != null && media.isNotEmpty) 'media': media,
      if (turnId != null) 'turn_id': turnId,
      'webui': true,
    });
  }

  /// Cancel the running task on [chatId] (server-side /stop semantics).
  void stopTask(String chatId) {
    // Clear the local busy flag only after the server acknowledges; the UI
    // keeps a bounded grace timer for the pathological case where the
    // gateway never answers.
    _confirmPendingSend(chatId, null);
    _send({
      'type': 'message',
      'chat_id': chatId,
      'content': '/stop',
      'webui': true,
      'turn_id': 'stop-${DateTime.now().microsecondsSinceEpoch}',
    });
  }

  /// Whether a turn for [chatId] was active and finalized since [listen].
  bool sawTurnEnd(String chatId) => _finalizedTurns.contains(chatId);

  // ---- Voice notes (audio -> text) ---------------------------------------

  final Map<String, Completer<String>> _transcriptions = {};
  int _transcriptionSeq = 0;

  /// Transcribe one recorded audio clip and return the text.
  ///
  /// The gateway owns the speech-to-text provider, so the APK never needs an
  /// on-device model or an API key. The clip is sent as a base64 data URL
  /// (the exact contract `webui_transcription_event` expects) and the reply
  /// arrives as `transcription_result` / `transcription_error`, correlated by
  /// `request_id`.
  ///
  /// Throws [StateError] with a user-readable message on failure so the
  /// composer can surface it instead of silently dropping the recording.
  Future<String> transcribeAudio({
    required String dataUrl,
    required int durationMs,
    Duration timeout = const Duration(seconds: 90),
  }) async {
    if (!isConnected) await connect();
    final requestId =
        'apk-tr-${DateTime.now().microsecondsSinceEpoch}-${_transcriptionSeq++}';
    final completer = Completer<String>();
    _transcriptions[requestId] = completer;
    _send({
      'type': 'transcribe_audio',
      if (_openChat != null) 'chat_id': _openChat!.chatId,
      'request_id': requestId,
      'data_url': dataUrl,
      'duration_ms': durationMs,
    });
    final timer = Timer(timeout, () {
      final c = _transcriptions.remove(requestId);
      if (c != null && !c.isCompleted) {
        c.completeError(
          StateError('Transcription timed out. Try a shorter voice note.'),
        );
      }
    });
    try {
      return await completer.future;
    } finally {
      timer.cancel();
      _transcriptions.remove(requestId);
    }
  }

  void _handleTranscriptionResult(Map<String, dynamic> ev) {
    final requestId = ev['request_id'];
    if (requestId is! String) return;
    final completer = _transcriptions.remove(requestId);
    if (completer == null || completer.isCompleted) return;
    final text = ev['text'];
    completer.complete(text is String ? text : '');
  }

  void _handleTranscriptionError(Map<String, dynamic> ev) {
    final requestId = ev['request_id'];
    if (requestId is! String) return;
    final completer = _transcriptions.remove(requestId);
    if (completer == null || completer.isCompleted) return;
    // The gateway sends a short machine reason ("mime", "duration", "size").
    final detail = ev['detail'];
    completer.completeError(
      StateError(_transcriptionMessage(detail is String ? detail : 'failed')),
    );
  }

  /// Map the gateway's short transcription reasons to something a user can act
  /// on. Falling back to the raw code keeps an unknown reason visible rather
  /// than swallowing it.
  static String _transcriptionMessage(String detail) {
    switch (detail) {
      case 'mime':
        return 'That audio format is not supported.';
      case 'duration':
        return 'That voice note is too long. Keep it under a couple of minutes.';
      case 'size':
        return 'That voice note is too large to upload.';
      case 'not_configured':
        return 'Speech-to-text is not enabled on the server yet.';
      case 'empty':
        return 'No speech detected — try again closer to the mic.';
      default:
        return 'Could not transcribe that voice note ($detail).';
    }
  }

  // ---- WebSocket mutations ----------------------------------------------
  //
  // Session deletion and other write actions are NOT reachable over plain
  // HTTP: the gateway answers 405 and routes them through the authenticated
  // WebSocket as `{"type":"webui_request","request_id":...,"action":...,
  // "payload":{...}}`, replying with a `webui_response` corrrelated by
  // request_id. This mirrors webui/src/lib/api.ts.

  final Map<String, Completer<_MutationReply>> _mutations = {};
  int _mutationSeq = 0;

  /// Run one allowlisted WebUI mutation over the socket and await its result.
  Future<Map<String, dynamic>> mutate(
    String action,
    Map<String, dynamic> payload, {
    Duration timeout = const Duration(seconds: 30),
  }) async {
    if (!isConnected) await connect();
    final requestId = 'apk-${DateTime.now().microsecondsSinceEpoch}-${_mutationSeq++}';
    final completer = Completer<_MutationReply>();
    _mutations[requestId] = completer;
    _send({
      'type': 'webui_request',
      'request_id': requestId,
      'action': action,
      'payload': payload,
    });
    final timer = Timer(timeout, () {
      final c = _mutations.remove(requestId);
      if (c != null && !c.isCompleted) {
        c.completeError(TimeoutException('$action timed out'));
      }
    });
    try {
      final reply = await completer.future;
      if (!reply.ok) {
        throw ApiException(reply.status ?? 500,
            reply.message ?? 'The server refused $action');
      }
      return reply.result;
    } finally {
      timer.cancel();
      _mutations.remove(requestId);
    }
  }

  /// Delete a chat session (and optionally its automations) via the socket.
  ///
  /// Returns the raw server payload so the caller can distinguish
  /// "deleted" from "blocked_by_automations" — the gateway answers success
  /// status for both.
  Future<DeleteSessionResult> deleteSession(
    String key, {
    bool deleteAutomations = false,
  }) async {
    final result = await mutate('session.delete', {
      'key': key,
      if (deleteAutomations) 'delete_automations': true,
    });
    return DeleteSessionResult.fromJson(result);
  }

  void _handleWebuiResponse(Map<String, dynamic> ev) {
    final requestId = ev['request_id'] as String?;
    if (requestId == null) return;
    final completer = _mutations.remove(requestId);
    if (completer == null || completer.isCompleted) return;
    final ok = ev['ok'] == true;
    Map<String, dynamic> result = const {};
    final rawResult = ev['result'];
    if (rawResult is Map) result = Map<String, dynamic>.from(rawResult);
    int? status;
    String? message;
    final err = ev['error'];
    if (err is Map) {
      if (err['status'] is num) status = (err['status'] as num).toInt();
      if (err['message'] is String) message = err['message'] as String;
    }
    completer.complete(_MutationReply(
        ok: ok, result: result, status: status, message: message));
  }

  void close() {
    _closedByUser = true;
    _reconnectTimer?.cancel();
    _reconnectTimer = null;
    _pendingFlushTimer?.cancel();
    _pendingFlushTimer = null;
    _stopHeartbeat();
    _sub?.cancel();
    _sub = null;
    try {
      _channel?.sink.close(ws_status.normalClosure);
    } catch (_) {}
    _channel = null;
    _views.clear();
    _wantedChats.clear();
    _attachedChats.clear();
    _activeTurns.clear();
    _completedTurnIds.clear();
    _outbox.clear();
  }
}