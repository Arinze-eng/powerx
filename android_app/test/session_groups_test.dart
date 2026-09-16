// Unit tests for the history-drawer grouping and filtering helpers.
//
// These are pure functions (no Flutter widgets), so they run fast and pin the
// recency bucketing rules the UI depends on.

import 'package:flutter_test/flutter_test.dart';
import 'package:powerx_android/models.dart';
import 'package:powerx_android/utils/session_groups.dart';

SessionSummary session({
  required String key,
  String title = '',
  String preview = '',
  DateTime? updatedAt,
}) {
  final chatId = key.contains(':') ? key.split(':').last : key;
  return SessionSummary(
    key: key,
    chatId: chatId,
    title: title,
    preview: preview,
    updatedAt: updatedAt,
  );
}

void main() {
  // Fixed reference instant so the buckets never depend on the wall clock.
  final now = DateTime(2026, 9, 16, 12, 0);

  group('groupSessions', () {
    test('buckets conversations into the expected recency labels', () {
      final rows = [
        session(key: 'websocket:a', title: 'today am', updatedAt: DateTime(2026, 9, 16, 9)),
        session(key: 'websocket:b', title: 'today pm', updatedAt: DateTime(2026, 9, 16, 11, 30)),
        session(key: 'websocket:c', title: 'yday', updatedAt: DateTime(2026, 9, 15, 20)),
        session(key: 'websocket:d', title: '4 days', updatedAt: DateTime(2026, 9, 12, 8)),
        session(key: 'websocket:e', title: '20 days', updatedAt: DateTime(2026, 8, 27, 8)),
        session(key: 'websocket:f', title: 'ancient', updatedAt: DateTime(2026, 1, 5, 8)),
      ];

      final groups = groupSessions(rows, now: now);

      expect(groups.map((g) => g.label).toList(), [
        kToday,
        kYesterday,
        kPrevious7Days,
        kPrevious30Days,
        kOlder,
      ]);
      expect(groups[0].rows.map((r) => r.title).toList(), ['today am', 'today pm']);
      expect(groups[1].rows.map((r) => r.title).toList(), ['yday']);
      expect(groups[2].rows.map((r) => r.title).toList(), ['4 days']);
      expect(groups[3].rows.map((r) => r.title).toList(), ['20 days']);
      expect(groups[4].rows.map((r) => r.title).toList(), ['ancient']);
    });

    test('drops empty buckets so no stray headers render', () {
      final groups = groupSessions(
        [session(key: 'websocket:a', updatedAt: DateTime(2026, 9, 16, 9))],
        now: now,
      );
      expect(groups.length, 1);
      expect(groups.single.label, kToday);
    });

    test('conversations without a timestamp remain reachable under Older', () {
      final groups = groupSessions(
        [session(key: 'websocket:x', title: 'no timestamp')],
        now: now,
      );
      expect(groups.single.label, kOlder);
      expect(groups.single.rows.single.title, 'no timestamp');
    });

    test('preserves the incoming order inside a bucket (server is authority)', () {
      final rows = [
        session(key: 'websocket:1', title: 'first', updatedAt: DateTime(2026, 9, 16, 8)),
        session(key: 'websocket:2', title: 'second', updatedAt: DateTime(2026, 9, 16, 7)),
        session(key: 'websocket:3', title: 'third', updatedAt: DateTime(2026, 9, 16, 6)),
      ];
      final groups = groupSessions(rows, now: now);
      expect(groups.single.rows.map((r) => r.title).toList(),
          ['first', 'second', 'third']);
    });

    test('a conversation counted as "yesterday" is not also counted today at midnight boundary', () {
      // 00:00 today must land in Today, not Yesterday.
      final groups = groupSessions(
        [session(key: 'websocket:mid', updatedAt: DateTime(2026, 9, 16, 0, 0))],
        now: now,
      );
      expect(groups.single.label, kToday);
    });

    test('empty input yields no groups', () {
      expect(groupSessions(const [], now: now), isEmpty);
    });
  });

  group('filterSessions', () {
    final rows = [
      session(key: 'websocket:a', title: 'Nuclear reactors', preview: 'summary of SMRs'),
      session(key: 'websocket:b', title: 'Spreadsheet cleanup', preview: 'csv renaming'),
    ];

    test('blank query returns everything', () {
      expect(filterSessions(rows, '   ').length, 2);
    });

    test('matches on title, case-insensitively', () {
      final out = filterSessions(rows, 'NUCLEAR');
      expect(out.single.title, 'Nuclear reactors');
    });

    test('matches on preview text too', () {
      final out = filterSessions(rows, 'renaming');
      expect(out.single.title, 'Spreadsheet cleanup');
    });

    test('no match yields an empty list', () {
      expect(filterSessions(rows, 'zzzz'), isEmpty);
    });
  });

  group('relativeDayLabel', () {
    test('shows a clock time for today', () {
      final label = relativeDayLabel(DateTime(2026, 9, 16, 9, 5), now: now);
      expect(label, '9:05 AM');
    });

    test('shows midnight as 12 AM, not 0 AM', () {
      expect(relativeDayLabel(DateTime(2026, 9, 16, 0, 30), now: now), '12:30 AM');
    });

    test('shows noon as 12 PM', () {
      expect(relativeDayLabel(DateTime(2026, 9, 16, 12, 0), now: now), '12:00 PM');
    });

    test('labels yesterday explicitly', () {
      expect(relativeDayLabel(DateTime(2026, 9, 15, 22), now: now), 'Yesterday');
    });

    test('uses a short date beyond a week', () {
      expect(relativeDayLabel(DateTime(2026, 3, 12, 8), now: now), '12 Mar');
    });
  });

  group('starter prompts', () {
    test('every starter carries a non-empty prompt and title', () {
      expect(starterPrompts, isNotEmpty);
      for (final p in starterPrompts) {
        expect(p.title.trim(), isNotEmpty);
        expect(p.prompt.trim(), isNotEmpty);
      }
    });

    test('starter prompts are distinct', () {
      final prompts = starterPrompts.map((p) => p.prompt).toSet();
      expect(prompts.length, starterPrompts.length);
    });
  });
}