// Unit tests for the PowerX native client core logic (no network required).

import 'package:flutter_test/flutter_test.dart';
import 'package:powerx_android/config.dart';
import 'package:powerx_android/models.dart';

void main() {
  group('PowerXConfig', () {
    test('exposes an HTTPS origin without trailing slash', () {
      expect(PowerXConfig.origin.startsWith('https://'), isTrue);
      expect(PowerXConfig.origin.endsWith('/'), isFalse);
    });

    test('derives a wss:// WebSocket origin from the https base', () {
      expect(PowerXConfig.wsOrigin.startsWith('wss://'), isTrue);
    });
  });

  group('SessionSummary', () {
    test('splits channel-prefixed keys into chatId', () {
      final s = SessionSummary.fromJson({
        'key': 'websocket:abc-123',
        'title': 'Trip planning',
        'preview': 'hello',
        'updated_at': '2026-09-15T10:00:00Z',
      });
      expect(s.chatId, 'abc-123');
      expect(s.displayTitle, 'Trip planning');
    });

    test('falls back to "New chat" when title is empty', () {
      final s = SessionSummary.fromJson({'key': 'c1'});
      expect(s.chatId, 'c1');
      expect(s.displayTitle, 'New chat');
    });
  });

  group('ThreadHistory.parse (live webui-thread schema)', () {
    test('maps user/reasoning/trace/answer messages into bubbles', () {
      final history = ThreadHistory.parse({
        'schemaVersion': 1,
        'sessionKey': 'websocket:abc',
        'active_turn_id': null,
        'has_pending_tool_calls': false,
        'messages': [
          {
            'id': 'u-1',
            'role': 'user',
            'content': 'hi there',
            'turnId': 't1',
            'turnPhase': 'user',
            'turnSeq': 1,
            'createdAt': 1789475645000,
          },
          {
            'id': 'as-2',
            'role': 'assistant',
            'content': '',
            'reasoning': 'The user said hi.',
            'turnId': 't1',
            'turnPhase': 'reasoning',
            'turnSeq': 15,
          },
          {
            'id': 'tool-3',
            'role': 'tool',
            'kind': 'trace',
            'content': 'list_dir({"path": "."})',
            'turnId': 't1',
            'turnPhase': 'activity',
            'turnSeq': 19,
          },
          {
            'id': 'as-4',
            'role': 'assistant',
            'content': 'Hello! How can I help?',
            'turnId': 't1',
            'turnPhase': 'answer',
            'turnSeq': 80,
            'latencyMs': 4200,
          },
        ],
      });
      expect(history.messages.length, 2);
      expect(history.messages.first.role, Role.user);
      final assistant = history.messages.last;
      expect(assistant.role, Role.assistant);
      expect(assistant.text, 'Hello! How can I help?');
      expect(assistant.reasoning, 'The user said hi.');
      expect(assistant.activity.length, 1);
      expect(assistant.activity.first.name, 'list_dir');
      expect(assistant.latencyMs, 4200);
    });

    test('groups traces + answers per turnId', () {
      final history = ThreadHistory.parse({
        'messages': [
          {'role': 'user', 'content': 'task A', 'turnPhase': 'user', 'turnId': 'a'},
          {'role': 'tool', 'kind': 'trace', 'content': 'run: step 1', 'turnPhase': 'activity', 'turnId': 'a'},
          {'role': 'assistant', 'content': 'answer A', 'turnPhase': 'answer', 'turnId': 'a'},
          {'role': 'user', 'content': 'task B', 'turnPhase': 'user', 'turnId': 'b'},
          {'role': 'assistant', 'content': 'answer B', 'turnPhase': 'answer', 'turnId': 'b'},
        ],
      });
      expect(history.messages.length, 4); // user, assistant(+1 step), user, assistant
      expect(history.messages[1].activity.length, 1);
      expect(history.messages[1].text, 'answer A');
      expect(history.messages[3].text, 'answer B');
    });

    test('keeps the answer text when the answer row also carries reasoning', () {
      // Regression: the real persisted transcript stamps the reasoning tail
      // onto the answer row. The old parser saw a non-empty `reasoning` and
      // treated the whole row as reasoning-only, dropping "LONG-OK-1" — the
      // reported "finished task shows no results after reopening the app".
      final history = ThreadHistory.parse({
        'schemaVersion': 2,
        'sessionKey': 'websocket:e2604311',
        'active_turn_id': null,
        'has_pending_tool_calls': true,
        'completed_turn_ids': ['t1'],
        'messages': [
          {
            'id': 'u-1',
            'role': 'user',
            'content': 'run sleep 60 then reply LONG-OK-1',
            'turnId': 't1',
            'turnPhase': 'user',
            'turnSeq': 1,
          },
          {
            'id': 'as-2',
            'role': 'assistant',
            'content': '',
            'reasoning': 'We need to run sleep 60 using the shell tool.',
            'turnId': 't1',
            'turnPhase': 'reasoning',
            'turnSeq': 42,
          },
          {
            'id': 'tr-3',
            'role': 'tool',
            'kind': 'trace',
            'content': 'novita_sandbox({"action": "run", "command": "sleep 60"})',
            'traces': ['novita_sandbox({"action": "run", "command": "sleep 60"})'],
            'toolEvents': [
              {
                'version': 1,
                'phase': 'end',
                'call_id': 'call-4c5e',
                'name': 'novita_sandbox',
                'arguments': {'action': 'run', 'command': 'sleep 60'},
                'result': '\n[exit_code=0]',
              },
            ],
            'turnId': 't1',
            'turnPhase': 'activity',
            'turnSeq': 46,
          },
          {
            'id': 'as-4',
            'role': 'assistant',
            'content': 'LONG-OK-1',
            'reasoning': 'Now reply exactly LONG-OK-1.',
            'turnId': 't1',
            'turnPhase': 'answer',
            'turnSeq': 54,
            'latencyMs': 163285,
          },
        ],
      });

      expect(history.messages.length, 2);
      final assistant = history.messages.last;
      expect(assistant.text, 'LONG-OK-1');
      expect(assistant.segments, ['LONG-OK-1']);
      expect(assistant.activity.length, 1);
      expect(assistant.activity.first.name, 'novita_sandbox');
      // Both reasoning fragments survive: the reasoning row and the tail the
      // server stamped onto the answer row.
      expect(assistant.reasoning, contains('We need to run sleep 60'));
      expect(assistant.reasoning, contains('Now reply exactly LONG-OK-1.'));
      expect(assistant.latencyMs, 163285);
    });

    test('orders a turn by turnSeq when every row carries one', () {
      final history = ThreadHistory.parse({
        'messages': [
          {'role': 'user', 'content': 'go', 'turnId': 't1', 'turnPhase': 'user', 'turnSeq': 1},
          {'role': 'assistant', 'content': 'done', 'turnId': 't1', 'turnPhase': 'answer', 'turnSeq': 30},
          {'role': 'tool', 'kind': 'trace', 'content': 'read_file({"path": "."})', 'turnId': 't1', 'turnPhase': 'activity', 'turnSeq': 12},
        ],
      });
      expect(history.messages.length, 2);
      final assistant = history.messages.last;
      expect(assistant.activity.length, 1);
      expect(assistant.text, 'done');
    });

    test('does not duplicate reasoning repeated on the answer row', () {
      final history = ThreadHistory.parse({
        'messages': [
          {'role': 'user', 'content': 'go', 'turnId': 't1', 'turnPhase': 'user', 'turnSeq': 1},
          {
            'role': 'assistant',
            'content': '',
            'reasoning': 'Only one thought.',
            'turnId': 't1',
            'turnPhase': 'reasoning',
            'turnSeq': 2,
          },
          {
            'role': 'assistant',
            'content': 'answer',
            'reasoning': 'Only one thought.',
            'turnId': 't1',
            'turnPhase': 'answer',
            'turnSeq': 3,
          },
        ],
      });
      final assistant = history.messages.last;
      expect(assistant.text, 'answer');
      expect(assistant.reasoning, 'Only one thought.');
    });

    test('reports active turn for resume detection', () {
      final history = ThreadHistory.parse({
        'messages': [
          {'role': 'user', 'content': 'go', 'turnPhase': 'user', 'turnId': 'x'},
        ],
        'active_turn_id': 'x',
        'has_pending_tool_calls': true,
      });
      expect(history.activeTurnId, 'x');
      expect(history.hasPendingToolCalls, isTrue);
    });

    test('tolerates malformed payloads', () {
      expect(ThreadHistory.parse(null).messages, isEmpty);
      expect(ThreadHistory.parse({}).messages, isEmpty);
      expect(ThreadHistory.parse({'messages': 'oops'}).messages, isEmpty);
    });
  });

  group('ChatMessage segments', () {
    test('accumulate multiple answer streams per turn', () {
      final m = ChatMessage(id: '1', role: Role.assistant, streaming: true);
      m.appendDelta('Hello ');
      m.appendDelta('world');
      m.endSegment('Hello world'); // authoritative text replaces live buffer
      m.appendDelta('Second part');
      expect(m.text, 'Hello world\n\nSecond part');
      m.dropEmptyTrailingSegment();
      expect(m.segments.length, 2);
    });

    test('late joiner heals from partial deltas via stream_end text', () {
      // A client that connected mid-stream has only partial text; the
      // stream_end event carries the full buffered text and heals the bubble.
      final late = ChatMessage(id: '2', role: Role.assistant, streaming: true);
      late.appendDelta('partial');
      late.endSegment('the full authoritative text');
      late.dropEmptyTrailingSegment();
      expect(late.text, 'the full authoritative text');
    });
  });

  group('ActivityStep.fromTraceLine', () {
    test('parses tool(name({args})) form', () {
      final s = ActivityStep.fromTraceLine('list_dir({"path": "."})', id: 'x');
      expect(s.name, 'list_dir');
      expect(s.detail, '.');
      expect(s.status, 'done');
    });

    test('falls back to whole line as name', () {
      final plain = ActivityStep.fromTraceLine('just a hint', id: 'y');
      expect(plain.name, 'just a hint');
      expect(plain.detail, isEmpty);
    });
  });
}
