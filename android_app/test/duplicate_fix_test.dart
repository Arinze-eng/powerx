// Regression tests for the "duplicate everything / results stop showing"
// fixes. Each group maps to a bug reported from the device:
//
//   1. Activity steps rendered 2-3x after close/reopen (the screenshot showed
//      `write_file · power_bank_guide.tex`, `edit · …`, `novita_sandbox · …`
//      repeated).
//   2. The answer bubble duplicated on reopen, because the live bubble had no
//      turn id until turn_end and was appended next to the server's copy.
//   3. Results stopped appearing: an authoritative-but-shorter final text was
//      discarded by a `text.length >=` guard.
//   4. Steps vanished on reopen: cached activity was only folded in when the
//      server bubble was *completely* empty.
//   5. A task typed just before the app was killed was lost, because the frame
//      only lived in the socket's in-memory outbox.
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:powerx_android/models.dart';
import 'package:powerx_android/services/chat_cache.dart';
import 'package:powerx_android/services/pending_sends.dart';

/// The five steps from the reported screenshot, as the live socket sends them
/// (real tool call ids).
List<ActivityStep> _liveSteps() => [
      ActivityStep(id: 'c1', name: 'write_file', detail: 'power_bank_guide.tex'),
      ActivityStep(id: 'c2', name: 'edit', detail: 'power_bank_guide.tex'),
      ActivityStep(
        id: 'c3',
        name: 'novita_sandbox',
        detail: 'cd /home/nanobot/.nanobot/workspace && pdflatex power_bank_guide.tex',
      ),
      ActivityStep(id: 'c4', name: 'novita_sandbox', detail: 'pwd && ls -la'),
      ActivityStep(
        id: 'c5',
        name: 'read_file',
        detail: '/home/nanobot/.nanobot/workspace/power_bank_guide.tex',
      ),
    ];

/// The same five steps as the persisted transcript replays them: identical
/// tool name + argument summary, but generated `trace-N` ids. This is the
/// mismatch that used to double (or triple) every row.
List<ActivityStep> _replayedSteps() => [
      for (var i = 0; i < 5; i++)
        ActivityStep(
          id: 'trace-$i',
          name: _liveSteps()[i].name,
          detail: _liveSteps()[i].detail,
          status: 'done',
          order: i,
        ),
    ];

void main() {
  group('activity dedupe (the repeated steps in the screenshot)', () {
    test('a replayed copy with different ids does not duplicate rows', () {
      final msg = ChatMessage(id: 'a1', role: Role.assistant);
      msg.activity.addAll(_liveSteps());

      final merged = mergeActivitySteps(msg.activity, _replayedSteps());

      expect(merged.length, 5, reason: 'exactly one row per tool call');
      expect(merged.map((s) => s.detail).toList(),
          _liveSteps().map((s) => s.detail).toList());
    });

    test('a third copy (cache) does not duplicate either', () {
      final msg = ChatMessage(id: 'a1', role: Role.assistant);
      var steps = mergeActivitySteps(msg.activity, _liveSteps());
      steps = mergeActivitySteps(steps, _replayedSteps());
      // The on-disk cache carried the live ids when it was written.
      steps = mergeActivitySteps(steps, _liveSteps());

      expect(steps.length, 5);
    });

    test('replaying the same batch twice in a row is idempotent', () {
      final msg = ChatMessage(id: 'a1', role: Role.assistant);
      var steps = mergeActivitySteps(msg.activity, _liveSteps());
      steps = mergeActivitySteps(steps, _liveSteps());
      expect(steps.length, 5);
    });

    test('a finished row is not reopened by a late running replay', () {
      final msg = ChatMessage(id: 'a1', role: Role.assistant);
      msg.activity.add(ActivityStep(
          id: 'c1', name: 'write_file', detail: 'a.tex', status: 'done'));

      final merged = mergeActivitySteps(msg.activity, [
        ActivityStep(
            id: 'trace-9', name: 'write_file', detail: 'a.tex', status: 'running'),
      ]);

      expect(merged.length, 1);
      expect(merged.single.status, 'done',
          reason: 'the most advanced status wins');
    });

    test('a real repeated tool call still shows as two rows', () {
      final existing = [
        ActivityStep(id: 'c1', name: 'run_command', detail: 'ls', status: 'done'),
      ];
      final merged = mergeActivitySteps(existing, [
        ActivityStep(id: 'c2', name: 'run_command', detail: 'ls', status: 'running'),
      ]);
      // Two distinct call ids that ran the same command must stay two rows;
      // only identical-id / replayed copies should collapse.
      expect(merged.length, 2);
    });

    test('a real tool call id is adopted over a synthetic replay id', () {
      final existing = [
        ActivityStep(id: 'trace-1', name: 'edit', detail: 'x.tex', status: 'done'),
      ];
      final merged = mergeActivitySteps(existing, [
        ActivityStep(id: 'cCall', name: 'edit', detail: 'x.tex', status: 'done'),
      ]);
      expect(merged.single.id, 'cCall');
    });

    test('toolKey ignores a truncated argument summary', () {
      final a = ActivityStep(id: '1', name: 'write_file', detail: 'a.tex');
      final b = ActivityStep(id: '2', name: 'write_file', detail: 'a.tex…');
      expect(a.toolKey, b.toolKey);
    });
  });

  group('authoritative final text (results must show)', () {
    test('a shorter final text replaces the streamed one', () {
      // This is the regression that stopped results appearing: the guard was
      // `text.length >= t.text.length`, so a correct short answer was dropped.
      final msg = ChatMessage(
          id: 'a1', role: Role.assistant, segments: ['a long partial answer']);
      msg.absorbFinalText('Done.');
      expect(msg.text, 'Done.');
    });

    test('an empty final text never erases what was streamed', () {
      final msg = ChatMessage(
          id: 'a1', role: Role.assistant, segments: ['the real answer']);
      msg.absorbFinalText('');
      msg.absorbFinalText(null);
      expect(msg.text, 'the real answer');
    });

    test('stream_end with no text keeps the streamed segment', () {
      final msg = ChatMessage(id: 'a1', role: Role.assistant);
      msg.appendDelta('accumulated');
      msg.endSegment(null);
      msg.dropEmptyTrailingSegment();
      expect(msg.text, 'accumulated');
    });

    test('a final message on a fresh bubble still displays', () {
      final msg = ChatMessage(id: 'a1', role: Role.assistant);
      msg.absorbFinalText('Power bank guide ready.');
      expect(msg.text, 'Power bank guide ready.');
    });
  });

  group('reopen merge (no duplicate chat, no lost steps/results)', () {
    /// A finished turn exactly as the gateway persists it.
    List<ChatMessage> serverThread() => [
          ChatMessage(id: 's-u1', role: Role.user, text: 'build the guide', turnId: 't1'),
          ChatMessage(
            id: 's-a1',
            role: Role.assistant,
            segments: ['The guide is ready.'],
            turnId: 't1',
          ),
        ];

    test('the answer is not duplicated when the live bubble had no turn id', () {
      // The reported duplicate: the app was killed mid-stream, so the cached
      // bubble never learned its server turn id.
      final cached = [
        ChatMessage(id: 'u-1', role: Role.user, text: 'build the guide'),
        ChatMessage(
            id: 'live-1', role: Role.assistant, segments: ['The guide is ready.']),
      ];

      final merged = mergeThreadHistory(server: serverThread(), cached: cached);

      expect(merged.length, 2, reason: 'one user row + one answer row');
      expect(merged.where((m) => m.role == Role.assistant).length, 1);
    });

    test('a killed-mid-stream turn keeps its steps without duplicating text', () {
      final cached = [
        ChatMessage(id: 'u-1', role: Role.user, text: 'build the guide'),
        ChatMessage(
          id: 'live-1',
          role: Role.assistant,
          segments: ['The guide is ready.'],
          activity: List.of(_liveSteps()),
        ),
      ];

      final merged = mergeThreadHistory(server: serverThread(), cached: cached);

      expect(merged.length, 2);
      final answer = merged.last;
      expect(answer.text, 'The guide is ready.');
      expect(answer.activity.length, 5, reason: 'steps survive the merge');
    });

    test('steps already on the server are not doubled by the cache', () {
      final server = [
        ChatMessage(id: 's-u1', role: Role.user, text: 'build', turnId: 't1'),
        ChatMessage(
          id: 's-a1',
          role: Role.assistant,
          segments: ['done'],
          turnId: 't1',
          activity: _replayedSteps(),
        ),
      ];
      final cached = [
        ChatMessage(
          id: 'live-1',
          role: Role.assistant,
          segments: ['done'],
          turnId: 't1',
          activity: List.of(_liveSteps()),
        ),
      ];

      final merged = mergeThreadHistory(server: server, cached: cached);

      expect(merged.last.activity.length, 5);
    });

    test('a partially persisted server turn still gains cached steps', () {
      // Previously activity was folded in only when the server bubble was
      // COMPLETELY empty, so a partially persisted turn lost every step.
      final server = [
        ChatMessage(id: 's-u1', role: Role.user, text: 'go', turnId: 't1'),
        ChatMessage(id: 's-a1', role: Role.assistant, segments: ['partial'], turnId: 't1'),
      ];
      final cached = [
        ChatMessage(
          id: 'live-1',
          role: Role.assistant,
          segments: ['partial'],
          turnId: 't1',
          activity: List.of(_liveSteps()),
        ),
      ];

      final merged = mergeThreadHistory(server: server, cached: cached);

      expect(merged.length, 2);
      expect(merged.last.text, 'partial');
      expect(merged.last.activity.length, 5);
    });

    test('an unfinished turn with no text still keeps its progress', () {
      final server = [
        ChatMessage(id: 's-u1', role: Role.user, text: 'go', turnId: 't1'),
      ];
      final cached = [
        ChatMessage(
            id: 'live-1', role: Role.assistant, activity: List.of(_liveSteps())),
      ];

      final merged = mergeThreadHistory(server: server, cached: cached);

      expect(merged.length, 2);
      expect(merged.last.activity.length, 5);
    });

    test('a truly unknown finished turn is preserved, not dropped', () {
      final server = [
        ChatMessage(id: 's-u1', role: Role.user, text: 'first', turnId: 't1'),
        ChatMessage(id: 's-a1', role: Role.assistant, segments: ['one'], turnId: 't1'),
      ];
      final cached = [
        ChatMessage(id: 'c-a9', role: Role.assistant, segments: ['two'], turnId: 't2'),
      ];

      final merged = mergeThreadHistory(server: server, cached: cached);

      expect(merged.map((m) => m.text), ['first', 'one', 'two']);
    });

    test('a cached echo of the user message is not repeated', () {
      final cached = [
        ChatMessage(id: 'u-1', role: Role.user, text: 'build the guide'),
      ];
      final merged = mergeThreadHistory(server: serverThread(), cached: cached);
      expect(merged.where((m) => m.role == Role.user).length, 1);
    });

    test('merging twice is stable (no growth on repeated reopens)', () {
      final cached = [
        ChatMessage(id: 'u-1', role: Role.user, text: 'build the guide'),
        ChatMessage(
          id: 'live-1',
          role: Role.assistant,
          segments: ['The guide is ready.'],
          activity: List.of(_liveSteps()),
        ),
      ];
      final once = mergeThreadHistory(server: serverThread(), cached: cached);
      final twice = mergeThreadHistory(server: serverThread(), cached: once);
      expect(twice.length, once.length);
      expect(twice.last.activity.length, 5);
    });

    test('cache round-trip keeps steps so a cold start shows them', () {
      final msg = ChatMessage(
        id: 'a1',
        role: Role.assistant,
        segments: ['ready'],
        turnId: 't1',
        activity: List.of(_liveSteps()),
      );
      final decoded = ChatCache.decode(ChatCache.encode([msg]));
      expect(decoded.single.activity.length, 5);
      expect(decoded.single.turnId, 't1');
    });
  });

  group('durable pending sends (a task must survive an app kill)', () {
    test('round-trips through the codec', () {
      final sends = [
        PendingSend(
          id: 'ps-1',
          chatId: 'chat-1',
          content: 'build the guide',
          media: [
            {'url': 'https://cdn/x.tex', 'name': 'x.tex'},
          ],
          turnId: 'turn-1',
          createdAtMs: 1000,
        ),
      ];
      final decoded = PendingSendQueue.decode(PendingSendQueue.encode(sends));
      expect(decoded.length, 1);
      expect(decoded.single.chatId, 'chat-1');
      expect(decoded.single.content, 'build the guide');
      expect(decoded.single.media?.single['name'], 'x.tex');
      expect(decoded.single.turnId, 'turn-1');
    });

    test('the wire frame matches the gateway message envelope', () {
      final send = PendingSend(
        id: 'ps-1',
        chatId: 'chat-1',
        content: '/stop',
        createdAtMs: 1,
      );
      final frame = send.toWireFrame();
      expect(frame['type'], 'message');
      expect(frame['chat_id'], 'chat-1');
      expect(frame['content'], '/stop');
      expect(frame['webui'], isTrue);
    });

    test('stale sends are dropped instead of starting a surprise task', () {
      final now = DateTime(2026, 9, 16, 12);
      final sends = [
        PendingSend(
          id: 'old',
          chatId: 'c1',
          content: 'yesterday task',
          createdAtMs: now
              .subtract(const Duration(hours: 20))
              .millisecondsSinceEpoch,
        ),
        PendingSend(
          id: 'fresh',
          chatId: 'c2',
          content: 'just typed',
          createdAtMs: now.millisecondsSinceEpoch,
        ),
      ];
      final pruned = PendingSendQueue.prune(sends, now: now);
      expect(pruned.map((s) => s.id), ['fresh']);
    });

    test('empty content without media is discarded', () {
      final now = DateTime(2026, 9, 16, 12);
      final pruned = PendingSendQueue.prune(
        [
          PendingSend(
            id: 'blank',
            chatId: 'c1',
            content: '   ',
            createdAtMs: now.millisecondsSinceEpoch,
          ),
        ],
        now: now,
      );
      expect(pruned, isEmpty);
    });

    test('the queue is bounded, keeping the newest entries', () {
      final now = DateTime(2026, 9, 16, 12);
      final sends = [
        for (var i = 0; i < PendingSendQueue.maxEntries + 5; i++)
          PendingSend(
            id: 'ps-$i',
            chatId: 'c$i',
            content: 'task $i',
            createdAtMs: now.millisecondsSinceEpoch,
          ),
      ];
      final pruned = PendingSendQueue.prune(sends, now: now);
      expect(pruned.length, PendingSendQueue.maxEntries);
      expect(pruned.last.content, 'task ${sends.length - 1}');
    });

    test('persists through the store and clears on demand', () async {
      final store = _MemoryStore();
      final queue = PendingSendQueue(store);
      await queue.save([
        PendingSend(
          id: 'ps-1',
          chatId: 'chat-1',
          content: 'do it',
          createdAtMs: DateTime.now().millisecondsSinceEpoch,
        ),
      ]);
      final loaded = await queue.load();
      expect(loaded.single.content, 'do it');
      await queue.clear();
      expect(await queue.load(), isEmpty);
    });

    test('tolerates a corrupt payload', () {
      expect(PendingSendQueue.decode('not json'), isEmpty);
      expect(PendingSendQueue.decode('{}'), isEmpty);
      expect(PendingSendQueue.decode('{"sends": 5}'), isEmpty);
      expect(PendingSendQueue.decode('{"sends": [1, 2]}'), isEmpty);
    });

    test('a stored payload is JSON-serializable (survives a real store)', () {
      final raw = PendingSendQueue.encode([
        PendingSend(
          id: 'ps-1',
          chatId: 'c1',
          content: 'x',
          createdAtMs: 1,
        ),
      ]);
      expect(jsonDecode(raw), isA<Map<String, dynamic>>());
    });
  });
}

class _MemoryStore implements KeyValueStore {
  final Map<String, String> _data = {};

  @override
  Future<String?> read(String key) async => _data[key];

  @override
  Future<void> write(String key, String value) async => _data[key] = value;

  @override
  Future<void> delete(String key) async => _data.remove(key);
}