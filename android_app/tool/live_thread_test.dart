// LIVE check — NOT part of the CI suite (lives outside test/).
//
// Verifies the app's own REST client against a real thread the production
// gateway already persisted, so a parser/route mismatch ("the chat shows
// nothing after reopening") cannot hide behind a green widget suite.
//
//   POWERX_E2E_CHAT=<chat id> flutter test tool/live_thread_test.dart
@TestOn('vm')
library;

// A manual probe: stdout is the point, so `print` is deliberate.
// ignore_for_file: avoid_print

import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:powerx_android/services/gateway_api.dart';

const String kOrigin = 'https://http--powerx--mxq9vl6k966n.code.run';

Future<String> signIn(String email, String password) async {
  final boot = jsonDecode((await http.get(Uri.parse('$kOrigin/webui/bootstrap'))).body)
      as Map<String, dynamic>;
  final sb = (boot['supabase'] as Map).cast<String, dynamic>();
  final r = await http.post(
    Uri.parse('${sb['url']}/auth/v1/token?grant_type=password'),
    headers: {'apikey': sb['anon_key'] as String, 'Content-Type': 'application/json'},
    body: jsonEncode({'email': email, 'password': password}),
  );
  expect(r.statusCode, 200, reason: r.body);
  return (jsonDecode(r.body) as Map<String, dynamic>)['access_token'] as String;
}

void main() {
  final email = Platform.environment['POWERX_E2E_EMAIL'] ?? 'apk-e2e@powerx.test';
  final password =
      Platform.environment['POWERX_E2E_PASSWORD'] ?? 'Apk-e2e-Str0ng!42';
  final chat = Platform.environment['POWERX_E2E_CHAT'] ?? '';

  test('the app\'s REST client reads a real persisted thread', () async {
    final api = GatewayApi();
    final sb = await signIn(email, password);
    final boot = await api.bootstrap(supabaseAccessToken: sb);
    print('api_token len=${boot.apiToken.length}');

    // Raw request first: shows the true status code, so a swallowed 404 in the
    // client cannot be mistaken for "the server has no transcript".
    final key = 'websocket:$chat';
    final raw = await http.get(
      Uri.parse('$kOrigin/api/sessions/${Uri.encodeComponent(key)}/webui-thread'
          '?limit=200&direction=latest'),
      headers: {'Authorization': 'Bearer ${boot.apiToken}', 'X-Nanobot-Auth': sb},
    );
    print('raw status=${raw.statusCode} bodyLen=${raw.body.length}');
    if (raw.statusCode == 200) {
      final decoded = jsonDecode(raw.body) as Map<String, dynamic>;
      final msgs = decoded['messages'] as List?;
      print('raw messages=${msgs?.length} '
          'active=${decoded['active_turn_id']} '
          'pending=${decoded['has_pending_tool_calls']} '
          'completed=${decoded['completed_turn_ids']}');
    }

    final hist = await api.fetchThread(boot.apiToken, key, supabaseToken: sb);
    print('parsed messages=${hist.messages.length} '
        'activeTurn=${hist.activeTurnId} pending=${hist.hasPendingToolCalls}');
    for (final m in hist.messages) {
      print('  ${m.role} text=${jsonEncode(m.text.length > 60 ? '${m.text.substring(0, 60)}…' : m.text)} '
          'activity=${m.activity.length} segments=${m.segments.length} '
          'turn=${m.turnId}');
    }
    expect(hist.messages, isNotEmpty,
        reason: 'a persisted thread must parse into bubbles');

    // The regression behind "a finished task shows no result after reopening":
    // every assistant row the server persisted with non-empty content must be
    // visible in the parsed transcript. A row that also carries `reasoning`
    // used to be swallowed whole.
    final rawMsgs = (jsonDecode(raw.body) as Map<String, dynamic>)['messages']
        as List? ?? const [];
    final expected = rawMsgs
        .whereType<Map>()
        .where((m) => (m['role'] ?? '') == 'assistant')
        .map((m) => (m['content'] ?? '').toString().trim())
        .where((t) => t.isNotEmpty)
        .toList();
    final got = hist.messages.map((m) => m.text.trim()).toList();
    for (final want in expected) {
      expect(got.any((t) => t.contains(want)), isTrue,
          reason: 'persisted answer ${jsonEncode(want)} is missing from the '
              'parsed transcript: $got');
      print('  answer visible: ${jsonEncode(want)}');
    }
  }, timeout: const Timeout(Duration(minutes: 3)));
}
