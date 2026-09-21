// LIVE probe — NOT part of the CI suite.
//
// It drives the real `NanobotSocket` against the production gateway to answer
// the questions the widget tests cannot: does a long turn actually stream, and
// what does the app get back after it dies mid-turn and is reopened?
//
// Run it manually (it needs network + a real account, so `flutter test` in CI
// must never collect it — that is why it lives under tool/ and not test/):
//
//   export POWERX_E2E_EMAIL=... POWERX_E2E_PASSWORD=...
//   flutter test tool/live_probe_test.dart
//
// Everything it prints is a timeline of what the production server sent.
@TestOn('vm')
library;

// A manual probe: stdout is the timeline, so `print` is deliberate.
// ignore_for_file: avoid_print

import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:powerx_android/config.dart';
import 'package:powerx_android/models.dart';
import 'package:powerx_android/services/gateway_api.dart';
import 'package:powerx_android/services/nanobot_socket.dart';

const String kOrigin = 'https://http--powerx--mxq9vl6k966n.code.run';

int _t0 = DateTime.now().millisecondsSinceEpoch;
String _ts() =>
    '+${((DateTime.now().millisecondsSinceEpoch - _t0) / 1000).toStringAsFixed(1)}s';

void log(String m) => print('[${_ts()}] $m');

Future<String> signIn(String email, String password) async {
  final boot = jsonDecode((await http.get(Uri.parse('$kOrigin/webui/bootstrap'))).body)
      as Map<String, dynamic>;
  final sb = (boot['supabase'] as Map).cast<String, dynamic>();
  final r = await http.post(
    Uri.parse('${sb['url']}/auth/v1/token?grant_type=password'),
    headers: {'apikey': sb['anon_key'] as String, 'Content-Type': 'application/json'},
    body: jsonEncode({'email': email, 'password': password}),
  );
  if (r.statusCode != 200) {
    throw StateError('sign-in failed ${r.statusCode}: ${r.body}');
  }
  return (jsonDecode(r.body) as Map<String, dynamic>)['access_token'] as String;
}

/// Records everything the socket delivers, with arrival times.
class Probe {
  final List<String> timeline = [];
  final StringBuffer answer = StringBuffer();
  bool ended = false;
  String? finalText;

  ChatView view() => ChatView(
        onDelta: (c) {
          answer.write(c);
          timeline.add('${_ts()} delta(${c.length})');
        },
        onReasoningDelta: (c) => timeline.add('${_ts()} reasoning(${c.length})'),
        onReasoningEnd: () => timeline.add('${_ts()} reasoning_end'),
        onStreamEnd: (t) {
          timeline.add('${_ts()} stream_end(${t?.length ?? 0})');
          if (t != null && t.isNotEmpty) finalText = t;
        },
        onActivity: (steps) =>
            timeline.add('${_ts()} activity(${steps.map((s) => s.name).join(",")})'),
        onTurnEnd: (s) {
          timeline.add('${_ts()} TURN_END usage=${s.usage}');
          ended = true;
        },
        onError: (d) => timeline.add('${_ts()} ERROR $d'),
        onUserMessage: (t, id) => timeline.add('${_ts()} user_message(${t.length})'),
        onFinalMessage: (t, m) {
          timeline.add('${_ts()} FINAL_MESSAGE(${t.length})');
          finalText = t;
          ended = true;
        },
        onUsage: (u) => timeline.add('${_ts()} usage($u)'),
      );
}

void main() {
  final email = Platform.environment['POWERX_E2E_EMAIL'] ?? 'apk-e2e@powerx.test';
  final password =
      Platform.environment['POWERX_E2E_PASSWORD'] ?? 'Apk-e2e-Str0ng!42';

  late String sbToken;
  late String apiToken;
  final api = GatewayApi();

  Future<WsToken> freshToken() async {
    final b = await api.bootstrap(supabaseAccessToken: sbToken);
    apiToken = b.apiToken;
    return WsToken(b.token, b.wsPath, supabaseToken: sbToken);
  }

  Future<NanobotSocket> openSocket() async {
    final s = NanobotSocket(
      wsBase: PowerXConfig.wsOrigin,
      tokenProvider: freshToken,
    );
    await s.connect();
    expect(s.isConnected, isTrue, reason: 'socket must connect');
    return s;
  }

  Future<void> settle(int seconds) async {
    final end = DateTime.now().add(Duration(seconds: seconds));
    while (DateTime.now().isBefore(end)) {
      await Future<void>.delayed(const Duration(milliseconds: 250));
    }
  }

  setUpAll(() async {
    _t0 = DateTime.now().millisecondsSinceEpoch;
    sbToken = await signIn(email, password);
    log('signed in as $email');
    final b = await api.bootstrap(supabaseAccessToken: sbToken);
    apiToken = b.apiToken;
    log('bootstrap ok model=${b.modelName} ws=${b.wsPath}');
  });

  test('1. a long, quiet turn streams to the creator socket', () async {
    final sock = await openSocket();
    final chatId = await sock.newChat();
    log('newChat -> $chatId');
    final p = Probe();
    sock.listen(chatId, p.view());
    await sock.attach(chatId);
    log('attached');
    sock.sendMessage(chatId,
        'Use your shell tool to run exactly: sleep 60    Then reply with exactly '
        'LONG-OK-1 and nothing else.');
    log('sent long task');

    final deadline = DateTime.now().add(const Duration(seconds: 280));
    while (!p.ended && DateTime.now().isBefore(deadline)) {
      await Future<void>.delayed(const Duration(milliseconds: 250));
    }
    log('turn ended=${p.ended}');
    for (final line in p.timeline) {
      log('  $line');
    }
    log('streamed text: ${p.answer.toString().trim()}');
    log('final text   : ${p.finalText}');

    // What the server persisted (this is what a reopen must render).
    final hist = await api.fetchThread(apiToken, 'websocket:$chatId');
    log('thread messages=${hist.messages.length} activeTurn=${hist.activeTurnId}');
    for (final m in hist.messages) {
      log('  ${m.role} len=${m.text.length} :: ${m.text.trim().substring(0, m.text.trim().length.clamp(0, 90))}');
    }
    sock.close();
    expect(p.ended, isTrue, reason: 'the long turn must end');
  }, timeout: const Timeout(Duration(minutes: 6)));

  test('2. a turn that outlives the app is recoverable on reopen', () async {
    final sock = await openSocket();
    final chatId = await sock.newChat();
    log('newChat -> $chatId');
    final p1 = Probe();
    sock.listen(chatId, p1.view());
    await sock.attach(chatId);
    sock.sendMessage(chatId,
        'Use your shell tool to run exactly: sleep 75    Then reply with exactly '
        'LONG-OK-2 and nothing else.');
    log('sent long task; holding the socket 25s then killing it');
    await settle(25);
    log('frames seen before the kill: ${p1.timeline.length}');
    sock.close();
    log('SOCKET CLOSED (simulates the app being killed mid-turn)');

    // Let the turn finish server-side while no client is listening.
    await settle(110);
    log('turn should be over server-side; reopening');

    final sock2 = await openSocket();
    final p2 = Probe();
    sock2.listen(chatId, p2.view());
    await sock2.attach(chatId);
    log('reopened + attached; what does the server replay?');
    await settle(45);
    for (final line in p2.timeline) {
      log('  replay $line');
    }
    log('replayed final text: ${p2.finalText}');

    final hist = await api.fetchThread(apiToken, 'websocket:$chatId');
    final texts = hist.messages.where((m) => m.role == Role.assistant).map((m) => m.text).join(' | ');
    log('thread assistant text: ${texts.trim()}');
    log('contains LONG-OK-2: ${texts.contains('LONG-OK-2')}');

    final sessions = await api.listSessions(apiToken);
    log('session listed: ${sessions.any((s) => s.chatId == chatId)}');
    sock2.close();
    expect(texts, contains('LONG-OK-2'),
        reason: 'the answer must be persisted so a reopen can show it');
  }, timeout: const Timeout(Duration(minutes: 8)));
}
