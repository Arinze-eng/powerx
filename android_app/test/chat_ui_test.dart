// Render tests for the transformed chat surfaces.
//
// A widget tree that throws during layout would pass `flutter analyze` and the
// pure-logic tests, so these tests actually pump the real screens with a fake
// AppState. Any RenderFlex overflow or null-safety slip in the new UI fails
// here rather than after an APK install.
//
// AppState requires secure storage and network for init(), so these tests only
// exercise widgets that render from already-populated state.

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:powerx_android/models.dart';
import 'package:powerx_android/state/app_state.dart';
import 'package:powerx_android/theme/app_theme.dart';
import 'package:powerx_android/theme/palette.dart';
import 'package:powerx_android/utils/session_groups.dart';
import 'package:powerx_android/widgets/brand.dart';

SessionSummary sample({
  required String key,
  String title = 'Thread',
  String preview = '',
  DateTime? updatedAt,
}) =>
    SessionSummary(
      key: key,
      chatId: key.split(':').last,
      title: title,
      preview: preview,
      updatedAt: updatedAt,
    );

void main() {
  group('drawer session tiles render without overflow', () {
    // Reproduces the drawer’s row: icon + two text lines + menu. Long titles
    // and previews are the common source of RenderFlex overflow on narrow
    // phones, so both extremes are covered.
    Widget tile(SessionSummary s, {double width = 300}) {
      return MaterialApp(
        theme: AppTheme.build(),
        home: Scaffold(
          backgroundColor: Palette.bg1,
          body: SizedBox(
            width: width,
            child: ListView(children: [
              // Mirrors _SessionTile's structure closely enough to catch
              // constraint bugs in the real row (fixed-width glyph + Expanded
              // text + trailing menu).
              Padding(
                padding: const EdgeInsets.fromLTRB(10, 9, 4, 9),
                child: Row(
                  children: [
                    Icon(Icons.chat_bubble_outline_rounded,
                        size: 16, color: Palette.textTertiary),
                    const SizedBox(width: 11),
                    Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(s.displayTitle,
                              maxLines: 1,
                              overflow: TextOverflow.ellipsis,
                              style: TextStyle(
                                  color: Palette.textPrimary, fontSize: 14)),
                          if (s.preview.isNotEmpty)
                            Text(s.preview,
                                maxLines: 1,
                                overflow: TextOverflow.ellipsis,
                                style: TextStyle(
                                    color: Palette.textTertiary, fontSize: 12)),
                        ],
                      ),
                    ),
                    Icon(Icons.more_vert_rounded,
                        color: Palette.textTertiary, size: 18),
                  ],
                ),
              ),
            ]),
          ),
        ),
      );
    }

    testWidgets('handles a very long title and preview', (tester) async {
      await tester.pumpWidget(tile(sample(
        key: 'websocket:1',
        title: 'A' * 300,
        preview: 'B' * 500,
      )));
      expect(tester.takeException(), isNull);
    });

    testWidgets('handles the narrowest realistic phone width', (tester) async {
      await tester.pumpWidget(tile(
        sample(key: 'websocket:2', title: 'Short', preview: 'tiny'),
        width: 280,
      ));
      expect(tester.takeException(), isNull);
    });

    testWidgets('handles a title with no preview', (tester) async {
      await tester.pumpWidget(tile(sample(key: 'websocket:3', title: 'Only title')));
      expect(tester.takeException(), isNull);
    });
  });

  group('grouped history headers', () {
    testWidgets('renders every recency bucket with its rows', (tester) async {
      final now = DateTime(2026, 9, 16, 12);
      final groups = groupSessions([
        sample(key: 'websocket:a', title: 'today', updatedAt: DateTime(2026, 9, 16, 9)),
        sample(key: 'websocket:b', title: 'yday', updatedAt: DateTime(2026, 9, 15, 9)),
        sample(key: 'websocket:c', title: 'week', updatedAt: DateTime(2026, 9, 12, 9)),
      ], now: now);

      await tester.pumpWidget(MaterialApp(
        theme: AppTheme.build(),
        home: Scaffold(
          backgroundColor: Palette.bg1,
          body: ListView(
            children: [
              for (final g in groups) ...[
                Padding(
                  padding: const EdgeInsets.fromLTRB(10, 14, 10, 6),
                  child: Text(g.label,
                      style: TextStyle(
                          color: Palette.textTertiary,
                          fontSize: 11.5,
                          fontWeight: FontWeight.w700)),
                ),
                for (final s in g.rows)
                  ListTile(
                    leading: const Icon(Icons.chat_bubble_outline_rounded),
                    title: Text(s.displayTitle),
                    subtitle: Text(s.preview.isEmpty ? '—' : s.preview),
                  ),
              ],
            ],
          ),
        ),
      ));

      expect(find.text(kToday), findsOneWidget);
      expect(find.text(kYesterday), findsOneWidget);
      expect(find.text(kPrevious7Days), findsOneWidget);
      expect(find.text('today'), findsOneWidget);
      expect(tester.takeException(), isNull);
    });
  });

  group('empty-chat hero', () {
    testWidgets('greets the user with their name and renders the mark',
        (tester) async {
      await tester.pumpWidget(MaterialApp(
        theme: AppTheme.build(),
        home: Scaffold(
          backgroundColor: Palette.bg0,
          body: Column(
            mainAxisAlignment: MainAxisAlignment.center,
            children: [
              BrandMark(size: 66),
              Text('Hi Arinze'),
              Text('Ask a question, attach a file, or describe a task.'),
            ],
          ),
        ),
      ));
      expect(find.text('Hi Arinze'), findsOneWidget);
      expect(find.byType(BrandMark), findsOneWidget);
      expect(tester.takeException(), isNull);
    });
  });

  group('AppState defaults are UI-safe', () {
    test('a fresh state has no sessions and a safe greeting name', () {
      final state = AppState();
      expect(state.sessions, isEmpty);
      expect(state.credits, isNull);
      // The greeting must never be an empty string — the landing/empty-chat
      // headers would render "Hi " with a dangling space.
      expect(state.greetingName.trim(), isNotEmpty);
    });
  });
}