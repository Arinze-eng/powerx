// One visible copy of every question and answer, across close/reopen cycles.
import 'package:flutter_test/flutter_test.dart';
import 'package:powerx_android/models.dart';
import 'package:powerx_android/services/chat_cache.dart';

ChatMessage _u(String id, String text, String turn) =>
    ChatMessage(id: id, role: Role.user, text: text, turnId: turn);

ChatMessage _a(String id, String text, String turn, {bool streaming = false}) {
  final m = ChatMessage(
    id: id,
    role: Role.assistant,
    segments: [text],
    turnId: turn,
    streaming: streaming,
  );
  return m;
}

List<String> _shape(List<ChatMessage> rows) => [
      for (final m in rows)
        '${m.role.name}:${m.turnId ?? '-'}:${m.text.trim()}',
    ];

void main() {
  group('mergeThreadHistory', () {
    test('merging the same cache again changes nothing (idempotent)', () {
      final server = [
        _u('s-u1', 'q1', 't1'),
        _a('s-a1', 'answer 1', 't1'),
      ];
      final cache = [
        _u('c-u1', 'q1', 't1'),
        _a('c-a1', 'answer 1', 't1'),
      ];
      final once = mergeThreadHistory(server: server, cached: cache);
      final twice = mergeThreadHistory(server: once, cached: cache);
      final thrice = mergeThreadHistory(server: twice, cached: cache);
      expect(_shape(once), _shape(twice));
      expect(_shape(twice), _shape(thrice));
      expect(once.length, 2);
    });

    test('a finished answer the server now holds is not shown twice', () {
      final server = [
        _u('s-u1', 'q1', 't1'),
        _a('s-a1', 'answer 1', 't1'),
      ];
      // The app was killed mid-stream: the cache has a partial answer, same turn.
      final cache = [
        _u('c-u1', 'q1', 't1'),
        _a('c-a1', 'answer', 't1', streaming: true),
      ];
      final merged = mergeThreadHistory(server: server, cached: cache);
      expect(merged.where((m) => m.role == Role.assistant).length, 1);
      expect(merged.where((m) => m.role == Role.user).length, 1);
    });

    test('the same question asked twice stays twice', () {
      final server = [
        _u('s-u1', 'hi', 't1'),
        _a('s-a1', 'hello', 't1'),
      ];
      final cache = [
        _u('c-u1', 'hi', 't1'),
        _a('c-a1', 'hello', 't1'),
        _u('c-u2', 'hi', 't2'),
        _a('c-a2', 'hello', 't2', streaming: true),
      ];
      final merged = mergeThreadHistory(server: server, cached: cache);
      final users = merged.where((m) => m.role == Role.user).toList();
      final answers = merged.where((m) => m.role == Role.assistant).toList();
      expect(users.map((m) => m.turnId), ['t1', 't2']);
      expect(answers.map((m) => m.turnId), ['t1', 't2']);
    });

    test('an identical older answer never swallows a new turn', () {
      // Server: the first turn answered "Done." A second turn is in flight and
      // has not been persisted yet. Its streamed "Done." must survive.
      final server = [
        _u('s-u1', 'do it', 't1'),
        _a('s-a1', 'Done.', 't1'),
      ];
      final cache = [
        _u('c-u2', 'do it', 't2'),
        _a('c-a2', 'Done.', 't2', streaming: true),
      ];
      final merged = mergeThreadHistory(server: server, cached: cache);
      final answers = merged.where((m) => m.role == Role.assistant).toList();
      expect(answers.length, 2);
      expect(answers.map((m) => m.turnId), ['t1', 't2']);
    });

    test('a question sent while the server is mid-turn stays below its turn',
        () {
      final server = [
        _u('s-u1', 'one', 't1'),
        _a('s-a1', 'answer one', 't1'),
      ];
      final cache = [
        _u('c-u1', 'one', 't1'),
        _a('c-a1', 'answer one', 't1'),
        _u('c-u2', 'two', 't2'),
        _a('c-a2', 'partial', 't2', streaming: true),
      ];
      final merged = mergeThreadHistory(server: server, cached: cache);
      expect(_shape(merged), [
        'user:t1:one',
        'assistant:t1:answer one',
        'user:t2:two',
        'assistant:t2:partial',
      ]);
    });
  });
}
