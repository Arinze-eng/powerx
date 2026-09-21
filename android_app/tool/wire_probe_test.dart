// WIRE probe — NOT part of the CI suite.
//
// Logs every websocket frame in both directions while reproducing the exact
// scenario the user reported: a long task, the app killed mid-turn, then the
// app reopened. This is the diagnostic that answers "did the message even
// reach the gateway, and what did the gateway send back after we came back?".
//
//   export POWERX_E2E_EMAIL=... POWERX_E2E_PASSWORD=...
//   flutter test tool/wire_probe_test.dart
@TestOn('vm')
library;

// A manual probe: stdout is the point.
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
  final boot = jsonDecode(
      (await http.get(Uri.parse('$kOrigin/webui/bootstrap'))).body) as Map<String, dynamic>;
  final sb = (boot['supabase'] as Map).cast<String, dynamic>();
  final r = await http.post(
    Uri.parse('${sb['url']}/auth/v1/token?grant_type=password'),
    headers: {
      'apikey': sb['anon_key'] as String,
      'Content-Type': 'application/json',
    },
    body: jsonEncode({'email': email, 'password': password}),
  );
  if (r.statusCode != 200) throw StateError('sign-in ${r.statusCode}: ${r.body}');
  return (jsonDecode(r.body) as Map<String, dynamic>)['access_token'] as String;
}

Future<void> settle(int seconds) async {
  final end = DateTime.now().add(Duration(seconds: seconds));
  while (DateTime.now().isBefore(end)) {
    await Future<void>.delayed(const Duration(milliseconds: 250));
  }
}

void main() {
  final email = Platform.environment['POWERX_E2E_EMAIL'] ?? 'apk-e2e@powerx.test';
  final password =
      Platform.environment['POWERX_E2E_PASSWORD'] ?? 'Apk-e2e-Str0ng!42';
  final api = GatewayApi();
  late String sbToken;
  late String apiToken;

  Future<NanobotSocket> openSocket(String tag) async {
    final s = NanobotSocket(
      wsBase: PowerXConfig.wsOrigin,
      tokenProvider: () async {
        final b = await api.bootstrap(supabaseAccessToken: sbToken);
        apiToken = b.apiToken;
        return WsToken(b.token, b.wsPath, supabaseToken: sbToken);
      },
    );
    s.frameLogger = (dir, frame) {
      final body = frame.length > 400 ? '${frame.substring(0, 400)}…' : frame;
      log('  [$tag $dir] $body');
    };
    await s.connect();
    return s;
  }

  setUpAll(() async {
    _t0 = DateTime.now().millisecondsSinceEpoch;
    sbToken = await signIn(email, password);
    final b = await api.bootstrap(supabaseAccessToken: sbToken);
    apiToken = b.apiToken;
    log('bootstrap ok model=${b.modelName} ws=${b.wsPath}');
  });

  test('wire trace: long task, app killed mid-turn, reopened', () async {
    final sock = await openSocket('A');
    final chatId = await sock.newChat();
    log('newChat -> $chatId connected=${sock.isConnected}');
    sock.listen(chatId, ChatView(
      onDelta: (c) => log('  A ui delta(${c.length})'),
      onReasoningDelta: (c) => log('  A ui reasoning(${c.length})'),
      onReasoningEnd: () {},
      onStreamEnd: (t) => log('  A ui stream_end(${t?.length ?? 0})'),
      onUserMessage: (t, id) => log('  A ui user_message(${t.length})'),
      onActivity: (s) => log('  A ui activity(${s.map((e) => e.name).join(",")})'),
      onTurnEnd: (s) => log('  A ui TURN_END'),
      onFinalMessage: (t, m) => log('  A ui FINAL(${t.length})'),
      onUsage: (u) => log('  A ui usage($u)'),
      onError: (d) => log('  A ui ERROR $d'),
    ));
    await sock.attach(chatId);
    log('attach A settled');
    sock.sendMessage(chatId,
        'Use your shell tool to run exactly: sleep 75    Then reply with exactly '
        'LONG-OK-2 and nothing else.');
    log('message frame handed to the socket');

    await settle(25);
    log('killing socket A now (app killed mid-turn)');
    sock.close();

    // Poll the server while nothing is listening, so we can see whether the
    // turn actually ran at all.
    for (var i = 0; i < 6; i++) {
      await settle(20);
      try {
        final hist = await api.fetchThread(apiToken, 'websocket:$chatId');
        final assistant = hist.messages
            .where((m) => m.role == Role.assistant)
            .map((m) => m.text.trim())
            .join(' | ');
        log('  poll +${(i + 1) * 20}s thread=${hist.messages.length} '
            'assistant="$assistant" active=${hist.activeTurnId}');
      } catch (e) {
        log('  poll +${(i + 1) * 20}s thread error $e');
      }
    }

    final sessions = await api.listSessions(apiToken);
    log('session listed after the turn: ${sessions.any((s) => s.chatId == chatId)}');

    log('reopening (app relaunched)');
    final sock2 = await openSocket('B');
    sock2.listen(chatId, ChatView(
      onDelta: (c) => log('  B ui delta(${c.length})'),
      onReasoningDelta: (c) => log('  B ui reasoning(${c.length})'),
      onReasoningEnd: () {},
      onStreamEnd: (t) => log('  B ui stream_end(${t?.length ?? 0})'),
      onUserMessage: (t, id) => log('  B ui user_message(${t.length})'),
      onActivity: (s) => log('  B ui activity(${s.map((e) => e.name).join(",")})'),
      onTurnEnd: (s) => log('  B ui TURN_END'),
      onFinalMessage: (t, m) => log('  B ui FINAL(${t.length})'),
      onUsage: (u) => log('  B ui usage($u)'),
      onError: (d) => log('  B ui ERROR $d'),
    ));
    await sock2.attach(chatId);
    log('attach B settled');
    await settle(20);
    final hist = await api.fetchThread(apiToken, 'websocket:$chatId');
    log('final thread messages=${hist.messages.length}');
    for (final m in hist.messages) {
      log('  ${m.role} activity=${m.activity.length} text="${m.text.trim()}"');
    }
    sock2.close();
  }, timeout: const Timeout(Duration(minutes: 8)));

  test('does a run survive when the socket that started it dies?', () async {
    final a = await openSocket('A');
    final chatId = await a.newChat();
    log('newChat -> $chatId');

    final b = await openSocket('B');
    b.listen(chatId, ChatView(
      onDelta: (c) => log('  B ui delta(${c.length})'),
      onReasoningDelta: (c) => log('  B ui reasoning(${c.length})'),
      onReasoningEnd: () {},
      onStreamEnd: (t) => log('  B ui stream_end(${t?.length ?? 0})'),
      onUserMessage: (t, id) => log('  B ui user_message(${t.length})'),
      onActivity: (s) => log('  B ui activity(${s.map((e) => e.name).join(",")})'),
      onTurnEnd: (s) => log('  B ui TURN_END'),
      onFinalMessage: (t, m) => log('  B ui FINAL(${t.length})'),
      onUsage: (u) => log('  B ui usage($u)'),
      onError: (d) => log('  B ui ERROR $d'),
    ));
    await b.attach(chatId);
    log('B attached (a second listener, like the webui tab)');

    a.listen(chatId, ChatView(
      onDelta: (c) => log('  A ui delta(${c.length})'),
      onReasoningDelta: (c) => log('  A ui reasoning(${c.length})'),
      onReasoningEnd: () {},
      onStreamEnd: (t) => log('  A ui stream_end(${t?.length ?? 0})'),
      onUserMessage: (t, id) => log('  A ui user_message(${t.length})'),
      onActivity: (s) => log('  A ui activity(${s.map((e) => e.name).join(",")})'),
      onTurnEnd: (s) => log('  A ui TURN_END'),
      onFinalMessage: (t, m) => log('  A ui FINAL(${t.length})'),
      onUsage: (u) => log('  A ui usage($u)'),
      onError: (d) => log('  A ui ERROR $d'),
    ));
    a.sendMessage(chatId,
        'Use your shell tool to run exactly: sleep 40    Then reply with exactly '
        'LONG-OK-3 and nothing else.');
    log('A sent the task');

    await settle(12);
    log('killing A; B stays attached');
    a.close();

    for (var i = 0; i < 6; i++) {
      await settle(20);
      try {
        final hist = await api.fetchThread(apiToken, 'websocket:$chatId');
        final assistant = hist.messages
            .where((m) => m.role == Role.assistant)
            .map((m) => m.text.trim())
            .join(' | ');
        log('  poll +${(i + 1) * 20}s thread=${hist.messages.length} '
            'assistant="$assistant"');
      } catch (e) {
        log('  poll +${(i + 1) * 20}s thread error $e');
      }
    }
    final sessions = await api.listSessions(apiToken);
    log('session listed: ${sessions.any((s) => s.chatId == chatId)}');
    b.close();
  }, timeout: const Timeout(Duration(minutes: 8)));
}
