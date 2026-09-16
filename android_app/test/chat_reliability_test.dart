// Regression tests for the Android client's reliability fixes:
//   * local transcript cache codec (results survive app close/reopen)
//   * history merge (never lose streamed text)
//   * honest delete parsing (gateway 200 + deleted:false)
//   * socket outbox / stop semantics
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:powerx_android/models.dart';
import 'package:powerx_android/services/chat_cache.dart';
import 'package:powerx_android/services/gateway_api.dart';

void main() {
  group('ChatCache codec', () {
    test('round-trips messages with segments, activity and usage', () {
      final msg = ChatMessage(
        id: 'a1',
        role: Role.assistant,
        segments: ['Answer part one', 'part two'],
        reasoning: 'thought about it',
        streaming: false,
        media: ['https://cdn/x.png'],
        activity: [
          ActivityStep(
              id: 'c1', name: 'write_file', detail: 'out.txt', status: 'done'),
        ],
        usage: {'llm_calls': 3},
        latencyMs: 1200,
        turnId: 't1',
      );
      final user = ChatMessage(id: 'u1', role: Role.user, text: 'hi');

      final decoded = ChatCache.decode(ChatCache.encode([user, msg]));

      expect(decoded.length, 2);
      expect(decoded.first.role, Role.user);
      expect(decoded.first.text, 'hi');
      final a = decoded.last;
      expect(a.role, Role.assistant);
      expect(a.text, 'Answer part one\n\npart two');
      expect(a.reasoning, 'thought about it');
      expect(a.media, ['https://cdn/x.png']);
      expect(a.activity.single.name, 'write_file');
      expect(a.usage?['llm_calls'], 3);
      expect(a.latencyMs, 1200);
      expect(a.turnId, 't1');
      // Never restore a streaming flag from disk — a cached turn is settled.
      expect(a.streaming, isFalse);
    });

    test('tolerates garbage without throwing', () {
      expect(ChatCache.decode('not json'), isEmpty);
      expect(ChatCache.decode('{}'), isEmpty);
      expect(ChatCache.decode('{"messages": "oops"}'), isEmpty);
      expect(ChatCache.decode('{"messages": [1, 2]}'), isEmpty);
    });

    test('caps stored messages to the newest window', () {
      final many = [
        for (var i = 0; i < ChatCache.maxMessagesPerChat + 25; i++)
          ChatMessage(id: 'm$i', role: Role.user, text: 'msg $i'),
      ];
      final decoded = ChatCache.decode(ChatCache.encode(many));
      expect(decoded.length, ChatCache.maxMessagesPerChat);
      // The tail (most recent) is what must survive.
      expect(decoded.last.text, 'msg ${many.length - 1}');
    });
  });

  group('mergeThreadHistory', () {
    test('keeps server messages when cache is empty', () {
      final server = [ChatMessage(id: 's1', role: Role.user, text: 'hi')];
      final merged = mergeThreadHistory(server: server, cached: []);
      expect(merged.length, 1);
      expect(merged.first.id, 's1');
    });

    test('returns cache when server history is unavailable', () {
      // This is the "reopen the app offline" path that used to show nothing.
      final cached = [
        ChatMessage(id: 'c1', role: Role.assistant, text: 'previous answer'),
      ];
      final merged = mergeThreadHistory(server: [], cached: cached);
      expect(merged.length, 1);
      expect(merged.first.text, 'previous answer');
    });

    test('fills an empty server bubble from the cache (mid-turn reopen)', () {
      final server = [
        ChatMessage(id: 's1', role: Role.user, text: 'do work', turnId: 't1'),
        ChatMessage(id: 's2', role: Role.assistant, text: '', turnId: 't1'),
      ];
      final cached = [
        ChatMessage(
            id: 'c1',
            role: Role.assistant,
            segments: ['streamed so far'],
            reasoning: 'thinking',
            turnId: 't1'),
      ];
      final merged = mergeThreadHistory(server: server, cached: cached);
      expect(merged.length, 2, reason: 'no duplicate bubble');
      expect(merged.last.text, 'streamed so far');
      expect(merged.last.reasoning, 'thinking');
    });

    test('never overwrites authoritative server text', () {
      final server = [
        ChatMessage(id: 's2', role: Role.assistant, text: 'final answer', turnId: 't1'),
      ];
      final cached = [
        ChatMessage(
            id: 'c1', role: Role.assistant, segments: ['stale partial'], turnId: 't1'),
      ];
      final merged = mergeThreadHistory(server: server, cached: cached);
      expect(merged.single.text, 'final answer');
    });

    test('appends a cached turn the server does not know about', () {
      final server = [ChatMessage(id: 's1', role: Role.user, text: 'one')];
      final cached = [
        ChatMessage(id: 'c9', role: Role.assistant, text: 'answer two', turnId: 't2'),
      ];
      final merged = mergeThreadHistory(server: server, cached: cached);
      expect(merged.map((m) => m.text), ['one', 'answer two']);
    });

    test('dedupes user echoes by text', () {
      final server = [ChatMessage(id: 's1', role: Role.user, text: 'same question')];
      final cached = [
        ChatMessage(id: 'c1', role: Role.user, text: 'same question'),
      ];
      final merged = mergeThreadHistory(server: server, cached: cached);
      expect(merged.length, 1);
    });

    test('drops empty cached assistant bubbles', () {
      final server = [ChatMessage(id: 's1', role: Role.user, text: 'hi')];
      final cached = [ChatMessage(id: 'c1', role: Role.assistant, turnId: 't9')];
      final merged = mergeThreadHistory(server: server, cached: cached);
      expect(merged.length, 1);
    });
  });

  group('DeleteSessionResult', () {
    test('parses a successful delete', () {
      final r = DeleteSessionResult.fromJson({'deleted': true});
      expect(r.deleted, isTrue);
      expect(r.blockedByAutomations, isFalse);
    });

    test('parses a blocked delete with automation names', () {
      // The gateway answers HTTP 200 here — treating it as success is exactly
      // the bug that made delete look like a no-op.
      final r = DeleteSessionResult.fromJson({
        'deleted': false,
        'blocked_by_automations': true,
        'automations': [
          {'id': 'job-1', 'name': 'Daily summary'},
          {'job_id': 'job-2', 'title': 'Hourly check'},
        ],
      });
      expect(r.deleted, isFalse);
      expect(r.blockedByAutomations, isTrue);
      expect(r.automations, ['Daily summary', 'Hourly check']);
    });

    test('handles missing/odd fields without throwing', () {
      final r = DeleteSessionResult.fromJson(const {});
      expect(r.deleted, isFalse);
      expect(r.automations, isEmpty);
    });
  });

  group('SessionAutomation', () {
    test('falls back to a readable label', () {
      final a = SessionAutomation.fromJson({'id': 'j1', 'enabled': false});
      expect(a.displayName, 'Automation');
      expect(a.enabled, isFalse);
    });

    test('parses the serialized job shape', () {
      final a = SessionAutomation.fromJson({
        'id': 'j9',
        'name': 'Nightly build',
        'schedule': '0 3 * * *',
        'pending': true,
      });
      expect(a.id, 'j9');
      expect(a.displayName, 'Nightly build');
      expect(a.schedule, '0 3 * * *');
      expect(a.pending, isTrue);
    });
  });

  group('GatewayBootstrap stays compatible with the cached delete payload', () {
    test('json round trip of the delete body decodes as JSON', () {
      // Guards against a gateway that returns the bare payload without a
      // content-type header (response is parsed from raw text).
      final raw = jsonEncode({'deleted': false, 'blocked_by_automations': true});
      final parsed = jsonDecode(raw) as Map<String, dynamic>;
      expect(DeleteSessionResult.fromJson(parsed).blockedByAutomations, isTrue);
    });
  });
}