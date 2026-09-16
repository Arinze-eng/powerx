// Socket-level integration tests for NanobotSocket against a local WebSocket
// server that replays the REAL event sequences captured from the live PowerX
// gateway (see tmp_test/events.jsonl). These lock in the critical protocol
// semantics that broke earlier builds:
//   * stream_end is per answer-stream, NOT per turn — the turn only ends on
//     turn_end / goal_status idle / final message. A coding task emits many.
//   * tool activity arrives as `message` events with kind=tool_hint/progress
//     and tool_events[] payloads.
//   * reasoning streams via reasoning_delta and closes with reasoning_end.
//   * a final `message` without kind carries the authoritative answer text
//     and must not be treated as an activity step.
//   * /stop: server emits goal_status running → idle (no turn_end) and a
//     standalone final message ("Stopped 1 task(s).") after idle.

@TestOn('vm')
library;

import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:powerx_android/models.dart';
import 'package:powerx_android/services/gateway_api.dart';
import 'package:powerx_android/services/nanobot_socket.dart';
import 'package:web_socket_channel/io.dart';
import 'package:web_socket_channel/web_socket_channel.dart';

/// A scripted fake gateway: accepts one connection, records inbound frames,
/// and can push server events on demand.
class FakeGateway {
  late final HttpServer _server;
  WebSocketChannel? _conn;
  final List<Map<String, dynamic>> inbound = [];

  Future<void> start() async {
    _server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
    _server.listen((req) async {
      if (WebSocketTransformer.isUpgradeRequest(req)) {
        final socket = await WebSocketTransformer.upgrade(req);
        final ch = IOWebSocketChannel(socket);
        _conn = ch;
        ch.stream.listen((raw) {
          final frame = jsonDecode(raw as String) as Map<String, dynamic>;
          inbound.add(frame);
          _handle(frame);
        });
      } else {
        req.response.statusCode = 404;
        await req.response.close();
      }
    });
  }

  String get wsUrl => 'ws://${_server.address.host}:${_server.port}';

  int get port => _server.port;

  /// How many `attach` frames the client sent for [chatId] on this connection.
  /// Used to prove a reconnect genuinely re-subscribes instead of
  /// short-circuiting on a stale "already attached" flag.
  int attachCountFor(String chatId) => inbound
      .where((f) => f['type'] == 'attach' && f['chat_id'] == chatId)
      .length;

  void _handle(Map<String, dynamic> frame) {
    switch (frame['type']) {
      case 'new_chat':
        send({'event': 'attached', 'chat_id': 'chat-A'});
        break;
      case 'attach':
        final ch = _conn!;
        ch.sink.add(jsonEncode(
            {'event': 'attached', 'chat_id': frame['chat_id']}));
        if (backgroundResume && frame['chat_id'] == resumeChatId) {
          resumeChatId = null; // fire once
          // Replay of an in-flight turn for a late joiner (what the real
          // gateway does via _hydrate_after_subscribe).
          ch.sink.add(jsonEncode({
            'event': 'goal_status',
            'chat_id': frame['chat_id'],
            'status': 'running',
          }));
          ch.sink.add(jsonEncode({
            'event': 'delta',
            'chat_id': frame['chat_id'],
            'text': ' resumed',
            'stream_id': 's9',
          }));
          ch.sink.add(jsonEncode({
            'event': 'stream_end',
            'chat_id': frame['chat_id'],
            'text': 'resumed text',
            'stream_id': 's9',
          }));
          ch.sink.add(jsonEncode({
            'event': 'turn_end',
            'chat_id': frame['chat_id'],
            'usage': {'llm_calls': 5},
            'latency_ms': 9000,
          }));
        }
        break;
    }
  }

  bool backgroundResume = false;
  String? resumeChatId;

  void send(Map<String, dynamic> ev) => _conn?.sink.add(jsonEncode(ev));

  Future<void> dropConnection() async => _conn?.sink.close();

  Future<void> stop() async {
    await _conn?.sink.close();
    await _server.close(force: true);
  }
}

/// A ChatView that records everything the socket delivers.
class Recorder {
  final List<String> deltas = [];
  final List<String> reasoningChunks = [];
  final List<List<ActivityStep>> activityBatches = [];
  final List<String?> streamEnds = [];
  final List<TurnSummary> turnEnds = [];
  final List<String> errors = [];
  final List<String> finalTexts = [];
  int reasoningEnds = 0;
  final List<String> userMessages = [];
  final List<Map<String, num>> usages = [];

  ChatView view() => ChatView(
        onDelta: (c) => deltas.add(c),
        onReasoningDelta: (c) => reasoningChunks.add(c),
        onReasoningEnd: () => reasoningEnds++,
        onStreamEnd: (t) => streamEnds.add(t),
        onActivity: (steps) => activityBatches.add(steps),
        onTurnEnd: (s) => turnEnds.add(s),
        onError: (d) => errors.add(d),
        onUserMessage: (t, id) => userMessages.add(t),
        onFinalMessage: (t, m) => finalTexts.add(t),
        onUsage: (u) {
          if (u != null) usages.add(u);
        },
      );
}

void main() {
  late FakeGateway gw;
  late NanobotSocket sock;

  setUp(() async {
    gw = FakeGateway();
    await gw.start();
    sock = NanobotSocket(
      wsBase: 'ws://127.0.0.1:${gw.port}',
      tokenProvider: () async => const WsToken('test-token', '/'),
    );
  });

  tearDown(() async {
    sock.close();
    await gw.stop();
  });

  test('turn survives multiple stream_end events; closes on turn_end only',
      () async {
    await sock.connect();
    final chatId = await sock.newChat();
    final rec = Recorder();
    sock.listen(chatId, rec.view());

    // Real-world sequence: two tool activity breadcrumbs, each followed by an
    // answer stream with its own stream_end, then a FINAL answer stream,
    // then one turn_end.
    sock.sendMessage(chatId, 'do the thing');
    await pumpEventQueue();

    gw.send({
      'event': 'goal_status',
      'chat_id': chatId,
      'status': 'running',
    });
    gw.send({
      'event': 'reasoning_delta',
      'chat_id': chatId,
      'text': 'I will list the dir',
    });
    gw.send({'event': 'reasoning_end', 'chat_id': chatId});
    gw.send({
      'event': 'message',
      'chat_id': chatId,
      'kind': 'tool_hint',
      'text': '',
      'tool_events': [
        {
          'version': 1,
          'phase': 'start',
          'call_id': 'c1',
          'name': 'list_dir',
          'arguments': {'path': '.'},
        },
        {
          'version': 1,
          'phase': 'end',
          'call_id': 'c1',
          'name': 'list_dir',
          'result': '…',
        },
      ],
    });
    // answer stream 1
    gw.send({'event': 'delta', 'chat_id': chatId, 'text': 'Working', 'stream_id': 's1'});
    gw.send({
      'event': 'stream_end',
      'chat_id': chatId,
      'text': 'Working',
      'stream_id': 's1',
    });
    // answer stream 2 (interleaved with tool activity) — the old client would
    // have DROPPED everything after the first stream_end.
    gw.send({'event': 'delta', 'chat_id': chatId, 'text': ' and done', 'stream_id': 's2'});
    gw.send({
      'event': 'message',
      'chat_id': chatId,
      'kind': 'progress',
      'text': '',
      'tool_events': [
        {
          'version': 1,
          'phase': 'start',
          'call_id': 'c2',
          'name': 'write_file',
          'arguments': {'path': 'a.txt'},
        },
      ],
    });
    gw.send({
      'event': 'stream_end',
      'chat_id': chatId,
      'text': ' and done',
      'stream_id': 's2',
    });
    await pumpEventQueue();

    // NOT terminal yet: no turn_end.
    expect(rec.turnEnds, isEmpty,
        reason: 'stream_end must not end the turn');
    expect(rec.deltas, ['Working', ' and done']);
    expect(rec.streamEnds, ['Working', ' and done']);
    expect(rec.activityBatches.length, 2);
    expect(rec.activityBatches[0].first.name, 'list_dir');
    expect(rec.activityBatches[0].first.status, 'done');
    expect(rec.reasoningChunks, ['I will list the dir']);
    expect(rec.reasoningEnds, 1);

    // Now the real terminators.
    gw.send({
      'event': 'turn_end',
      'chat_id': chatId,
      'usage': {'llm_calls': 3, 'prompt_tokens': 100},
      'latency_ms': 2500,
    });
    await pumpEventQueue();
    expect(rec.turnEnds.length, 1);
    expect(rec.turnEnds.single.usage?['llm_calls'], 3);
    expect(rec.turnEnds.single.latencyMs, 2500);

    // A duplicate idle afterwards must not fire turn_end twice.
    gw.send({'event': 'goal_status', 'chat_id': chatId, 'status': 'idle'});
    await pumpEventQueue();
    expect(rec.turnEnds.length, 1);
  });

  test('final message event is authoritative text and ends the turn',
      () async {
    await sock.connect();
    final chatId = await sock.newChat();
    final rec = Recorder();
    sock.listen(chatId, rec.view());
    sock.sendMessage(chatId, 'hello');
    gw.send({'event': 'goal_status', 'chat_id': chatId, 'status': 'running'});
    gw.send({'event': 'delta', 'chat_id': chatId, 'text': 'par', 'stream_id': 's1'});
    gw.send({
      'event': 'message',
      'chat_id': chatId,
      'text': 'The full authoritative answer',
      'media_urls': [
        {'path': 'p', 'url': 'https://cdn/x.png', 'contentType': 'image/png'}
      ],
      'usage': {'llm_calls': 2},
    });
    await pumpEventQueue();
    expect(rec.finalTexts, ['The full authoritative answer']);
    expect(rec.turnEnds.length, 1);
  });

  test('cancel path: goal idle ends turn; post-idle stop ack still delivered',
      () async {
    await sock.connect();
    final chatId = await sock.newChat();
    final rec = Recorder();
    sock.listen(chatId, rec.view());
    sock.sendMessage(chatId, 'long task');
    gw.send({'event': 'goal_status', 'chat_id': chatId, 'status': 'running'});
    gw.send({'event': 'delta', 'chat_id': chatId, 'text': 'streaming…', 'stream_id': 's1'});
    // /stop: running (system turn), then idle (terminal, NO turn_end),
    // then the acknowledgement message arrives afterwards.
    gw.send({'event': 'goal_status', 'chat_id': chatId, 'status': 'running'});
    gw.send({'event': 'goal_status', 'chat_id': chatId, 'status': 'idle'});
    await pumpEventQueue();
    expect(rec.turnEnds.length, 1, reason: 'idle must be terminal');
    gw.send({
      'event': 'message',
      'chat_id': chatId,
      'text': 'Stopped 1 task(s).',
    });
    await pumpEventQueue();
    expect(rec.finalTexts, ['Stopped 1 task(s).']);
  });

  test('reconnect re-attaches previous chats so background turns resume',
      () async {
    await sock.connect();
    final chatId = await sock.newChat();
    final rec = Recorder();
    sock.listen(chatId, rec.view());
    sock.sendMessage(chatId, 'bg task');
    gw.send({'event': 'goal_status', 'chat_id': chatId, 'status': 'running'});
    await pumpEventQueue();

    // Configure the fake gateway to replay a running turn for late joiners,
    // then simulate the network dropping the connection.
    gw.backgroundResume = true;
    gw.resumeChatId = chatId;
    await gw.dropConnection();
    await Future<void>.delayed(const Duration(milliseconds: 150));

    // The client auto-reconnects (backoff) and re-attaches previous chats;
    // the replayed running turn must flow into the same recorder.
    final deadline = DateTime.now().add(const Duration(seconds: 30));
    while (rec.turnEnds.isEmpty && DateTime.now().isBefore(deadline)) {
      await Future<void>.delayed(const Duration(milliseconds: 100));
    }
    expect(gw.inbound.any((f) => f['type'] == 'attach'), isTrue);
    expect(rec.deltas.any((d) => d.contains('resumed')), isTrue);
    expect(rec.turnEnds.any((t) => t.usage?['llm_calls'] == 5), isTrue);
  });

  test('stale idle replay terminates busy state armed from history snapshot',
      () async {
    await sock.connect();
    final chatId = await sock.newChat();
    final rec = Recorder();
    sock.listen(chatId, rec.view());
    // Reproduce the stuck-spinner scenario: the UI armed its busy indicator
    // from a history snapshot (activeTurnId), but the turn already finished
    // server-side before the subscribe. The attach replay of goal_status idle
    // must still terminate the turn exactly once.
    gw.send({'event': 'goal_status', 'chat_id': chatId, 'status': 'idle'});
    await pumpEventQueue();
    expect(rec.turnEnds.length, 1,
        reason: 'stale idle must terminate a turn the UI thinks is running');
    // A second idle (duplicate replay) must NOT fire a second turn_end.
    gw.send({'event': 'goal_status', 'chat_id': chatId, 'status': 'idle'});
    await pumpEventQueue();
    expect(rec.turnEnds.length, 1, reason: 'duplicate terminal events ignored');
  });

  test('duplicate turn_end fires onTurnEnd exactly once', () async {
    await sock.connect();
    final chatId = await sock.newChat();
    final rec = Recorder();
    sock.listen(chatId, rec.view());
    sock.sendMessage(chatId, 'hello');
    gw.send({'event': 'goal_status', 'chat_id': chatId, 'status': 'running'});
    await pumpEventQueue();
    gw.send({'event': 'turn_end', 'chat_id': chatId});
    await pumpEventQueue();
    gw.send({'event': 'turn_end', 'chat_id': chatId});
    await pumpEventQueue();
    expect(rec.turnEnds.length, 1);
  });

  test('ChatMessage.artifactPaths derives download-ready file paths', () {
    final msg = ChatMessage(id: 'm1', role: Role.assistant);
    expect(msg.artifactPaths, isEmpty);
    msg.activity.add(ActivityStep(
        id: 'a1', name: 'write_file', detail: 'reports/summary.md',
        status: 'done', order: 0));
    msg.activity.add(ActivityStep(
        id: 'a2', name: 'write_file', detail: 'reports/summary.md',
        status: 'done', order: 1)); // duplicate path deduped
    msg.activity.add(ActivityStep(
        id: 'a3', name: 'read_file', detail: 'notes.md',
        status: 'done', order: 2)); // reads are not artifacts
    msg.activity.add(ActivityStep(
        id: 'a4', name: 'web_search', detail: 'https://example.com/x',
        status: 'done', order: 3));
    expect(msg.artifactPaths, ['reports/summary.md']);
  });

  test('a quiet long-running turn is never cut short by the client', () async {
    await sock.connect();
    final chatId = await sock.newChat();
    final rec = Recorder();
    sock.listen(chatId, rec.view());
    sock.sendMessage(chatId, 'very long build');
    gw.send({'event': 'goal_status', 'chat_id': chatId, 'status': 'running'});
    gw.send({'event': 'message', 'chat_id': chatId, 'kind': 'tool_hint',
      'text': '', 'tool_events': [
        {'phase': 'start', 'call_id': 'c1', 'name': 'run_command',
         'arguments': {'command': 'make build'}},
      ]});
    await pumpEventQueue();

    // Simulate a long, silent tool run: no events for a long time. The turn
    // must stay active (the old 90 s watchdog force-cleared it and showed the
    // task as "cut off" mid-work).
    await Future<void>.delayed(const Duration(seconds: 2));
    expect(rec.turnEnds, isEmpty);
    expect(sock.isTurnActive(chatId), isTrue);

    // The work eventually reports completion.
    gw.send({'event': 'goal_status', 'chat_id': chatId, 'status': 'idle'});
    await pumpEventQueue();
    expect(rec.turnEnds.length, 1);
    expect(sock.isTurnActive(chatId), isFalse);
  });

  test('stop sends /stop without locally cancelling the turn', () async {
    await sock.connect();
    final chatId = await sock.newChat();
    final rec = Recorder();
    sock.listen(chatId, rec.view());
    sock.sendMessage(chatId, 'long task');
    gw.send({'event': 'goal_status', 'chat_id': chatId, 'status': 'running'});
    await pumpEventQueue();

    sock.stopTask(chatId);
    await pumpEventQueue();

    // The cancel request reached the server...
    expect(
        gw.inbound.any((f) => f['type'] == 'message' && f['content'] == '/stop'),
        isTrue);
    // ...and the client did NOT assume the turn ended before the server said
    // so (a server-authoritative stop is what prevents "it just gets cut").
    expect(rec.turnEnds, isEmpty);

    gw.send({'event': 'goal_status', 'chat_id': chatId, 'status': 'idle'});
    await pumpEventQueue();
    expect(rec.turnEnds.length, 1);
  });

  test('frames sent while offline are queued and flushed on reconnect',
      () async {
    await sock.connect();
    final chatId = await sock.newChat();
    await gw.dropConnection();
    await Future<void>.delayed(const Duration(milliseconds: 80));

    // Dropped while the socket is down: the frame must be buffered, not lost.
    sock.stopTask(chatId);
    expect(sock.isConnected, isFalse);

    // The client auto-reconnects and replays the queued frame.
    final deadline = DateTime.now().add(const Duration(seconds: 30));
    while (DateTime.now().isBefore(deadline)) {
      if (gw.inbound.any((f) => f['content'] == '/stop')) break;
      await Future<void>.delayed(const Duration(milliseconds: 100));
    }
    expect(gw.inbound.any((f) => f['content'] == '/stop'), isTrue);
  });

  test('an error for a chat without a view is not silently swallowed',
      () async {
    await sock.connect();
    final chatId = await sock.newChat();
    String? seenChat;
    String? seenDetail;
    sock.onErrorEvent = (c, d) {
      seenChat = c;
      seenDetail = d;
    };
    gw.send({'event': 'error', 'chat_id': chatId, 'detail': 'tool_failed'});
    await pumpEventQueue();
    expect(seenChat, chatId);
    expect(seenDetail, 'tool_failed');
  });

  test('protocol frames are classified so they never become transcript rows',
      () {
    expect(isProtocolFrame(jsonEncode({'event': 'attached'})), isTrue);
    expect(isProtocolFrame(jsonEncode({'event': 'goal_status'})), isTrue);
    expect(isProtocolFrame(jsonEncode({'event': 'delta', 'text': 'x'})), isFalse);
    expect(isProtocolFrame('not json'), isFalse);
  });

  test('re-subscribes after a drop so a backgrounded turn resumes', () async {
    // Regression: the confirmed-attach set used to survive a disconnect, so
    // attach() short-circuited after a reconnect and the client NEVER
    // re-subscribed — the server never replayed goal_status: running and the
    // UI sat frozen ("everything stucks after leaving the app").
    await sock.connect();
    // Open an EXISTING conversation (the resume path: ChatScreen.attach).
    const chatId = 'chat-A';
    await sock.attach(chatId);
    final rec = Recorder();
    sock.listen(chatId, rec.view());
    await pumpEventQueue();
    final attachesBefore = gw.attachCountFor(chatId);
    expect(attachesBefore, greaterThanOrEqualTo(1));

    // Background the app: the socket dies without a clean close.
    await gw.dropConnection();
    await Future<void>.delayed(const Duration(milliseconds: 80));

    // Come back to the foreground and re-attach (what _recoverOnResume does).
    await sock.checkLiveness(maxIdle: Duration.zero);
    await sock.attach(chatId);
    await pumpEventQueue();

    // The attach must actually reach the server again...
    expect(gw.attachCountFor(chatId), greaterThan(attachesBefore),
        reason: 'reconnect must re-subscribe, not short-circuit');

    // ...and the running turn is replayed, so the task visibly resumes.
    gw.send({'event': 'goal_status', 'chat_id': chatId, 'status': 'running',
      'started_at': 1000.0});
    gw.send({'event': 'delta', 'chat_id': chatId, 'text': 'resumed output'});
    await pumpEventQueue();
    expect(rec.deltas, contains('resumed output'));
  });

  test('attach replay of a finished turn ends the stale busy indicator',
      () async {
    // A turn can finish between the history fetch and the subscribe. The
    // replayed idle must still fire turn_end exactly once so the green pill
    // never rolls forever.
    await sock.connect();
    final chatId = await sock.newChat();
    final rec = Recorder();
    sock.listen(chatId, rec.view());
    gw.send({'event': 'goal_status', 'chat_id': chatId, 'status': 'idle'});
    await pumpEventQueue();
    expect(rec.turnEnds.length, 1);

    // Duplicate terminal events must not re-fire it.
    gw.send({'event': 'goal_status', 'chat_id': chatId, 'status': 'idle'});
    await pumpEventQueue();
    expect(rec.turnEnds.length, 1);
  });

  test('delete uses the websocket mutation transport, not plain HTTP',
      () async {
    // Regression: the HTTP route answers 405 ("WebUI mutations require an
    // authenticated WebSocket"), which surfaced as an error when deleting a
    // chat. Delete must go out as a webui_request envelope.
    await sock.connect();
    final future = sock.deleteSession('websocket:chat-1');
    await pumpEventQueue();
    final frame = gw.inbound.lastWhere(
        (f) => f['type'] == 'webui_request' && f['action'] == 'session.delete');
    expect(frame['payload']['key'], 'websocket:chat-1');

    gw.send({
      'event': 'webui_response',
      'request_id': frame['request_id'],
      'ok': true,
      'result': {'deleted': true},
    });
    final result = await future;
    expect(result.deleted, isTrue);
  });

  test('a blocked delete is reported with its automation names', () async {
    await sock.connect();
    final future = sock.deleteSession('websocket:chat-2');
    await pumpEventQueue();
    final frame = gw.inbound.lastWhere(
        (f) => f['type'] == 'webui_request' && f['action'] == 'session.delete');
    gw.send({
      'event': 'webui_response',
      'request_id': frame['request_id'],
      'ok': true,
      'result': {
        'deleted': false,
        'blocked_by_automations': true,
        'automations': [{'id': 'j1', 'name': 'Daily digest'}],
      },
    });
    final result = await future;
    expect(result.deleted, isFalse);
    expect(result.blockedByAutomations, isTrue);
    expect(result.automations, ['Daily digest']);
  });

  test('a refused mutation surfaces as an ApiException', () async {
    await sock.connect();
    final future = sock.deleteSession('websocket:chat-3');
    await pumpEventQueue();
    final frame = gw.inbound.lastWhere(
        (f) => f['type'] == 'webui_request' && f['action'] == 'session.delete');
    gw.send({
      'event': 'webui_response',
      'request_id': frame['request_id'],
      'ok': false,
      'error': {'status': 404, 'message': 'session not found'},
    });
    await expectLater(
        future, throwsA(isA<ApiException>().having((e) => e.status, 'status', 404)));
  });

  test('the client never sends a bare ping frame', () async {
    // Regression: {"type":"ping"} made the gateway answer
    // `error: unknown type: 'ping'`, which users saw as an error toast.
    await sock.connect();
    final chatId = await sock.newChat();
    sock.listen(chatId, Recorder().view());
    await Future<void>.delayed(const Duration(milliseconds: 50));
    expect(gw.inbound.any((f) => f['type'] == 'ping'), isFalse);
    expect(gw.inbound.every((f) => f['type'] != 'ping'), isTrue);
  });
}
