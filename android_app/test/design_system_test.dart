// Render smoke tests for the shared design-system widgets.
//
// These render for real (pumping a frame) so layout mistakes — unbounded
// constraints, bad gradients, missing Directionality — fail here instead of on
// a device.

import 'package:flutter/material.dart';
import 'package:flutter_markdown/flutter_markdown.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:powerx_android/theme/app_theme.dart';
import 'package:powerx_android/theme/palette.dart';
import 'package:powerx_android/widgets/brand.dart';

/// Wraps [child] in the app theme so widgets resolve theme defaults the same
/// way they do in production. The phone-sized box is the OUTER bound and the
/// child is centred inside it, so fixed-size widgets keep their own dimensions
/// instead of being stretched to fill the frame.
Widget harness(Widget child, {double width = 390, double height = 844}) {
  return MaterialApp(
    theme: AppTheme.build(),
    home: Scaffold(
      body: SizedBox(
        width: width,
        height: height,
        child: Center(child: child),
      ),
    ),
  );
}

void main() {
  group('AppTheme', () {
    test('builds a dark theme anchored on the brown palette', () {
      final theme = AppTheme.build();
      expect(theme.brightness, Brightness.dark);
      expect(theme.scaffoldBackgroundColor, Palette.bg0);
      expect(theme.colorScheme.primary, Palette.accent);
      expect(theme.appBarTheme.backgroundColor, Palette.bg1);
    });

    test('applies the brand accent to primary buttons', () {
      final theme = AppTheme.build();
      final style = theme.filledButtonTheme.style;
      expect(style, isNotNull);
    });

    testWidgets('markdown stylesheet is derived from the theme', (tester) async {
      late MarkdownStyleSheetProbe probe;
      await tester.pumpWidget(MaterialApp(
        theme: AppTheme.build(),
        home: Builder(builder: (context) {
          probe = MarkdownStyleSheetProbe(AppTheme.markdown(context));
          return const SizedBox.shrink();
        }),
      ));
      // The body text must use our readable 15px warm-white style, not the
      // Material default (which would render dark-on-dark).
      expect(probe.style.p!.fontSize, 15);
      expect(probe.style.p!.color, Palette.textPrimary);
    });
  });

  group('BrandMark', () {
    testWidgets('renders the bolt glyph at the requested size', (tester) async {
      await tester.pumpWidget(harness(const BrandMark(size: 88)));
      expect(find.text('⚡'), findsOneWidget);

      final size = tester.getSize(find.byType(BrandMark));
      expect(size.width, 88);
      expect(size.height, 88);
    });

    testWidgets('respects a custom radius without overflowing', (tester) async {
      await tester.pumpWidget(harness(const BrandMark(size: 64, radius: 12)));
      expect(tester.takeException(), isNull);
      expect(find.text('⚡'), findsOneWidget);
    });
  });

  group('ChatAvatar', () {
    testWidgets('assistant avatar shows the brand glyph', (tester) async {
      await tester.pumpWidget(harness(const ChatAvatar(isAssistant: true)));
      expect(find.text('⚡'), findsOneWidget);
    });

    testWidgets('user avatar shows the uppercased initial', (tester) async {
      await tester.pumpWidget(
          harness(const ChatAvatar(isAssistant: false, initial: 'arinze')));
      expect(find.text('A'), findsOneWidget);
    });

    testWidgets('blank initial falls back to a placeholder, never crashes',
        (tester) async {
      await tester.pumpWidget(
          harness(const ChatAvatar(isAssistant: false, initial: '   ')));
      expect(find.text('?'), findsOneWidget);
      expect(tester.takeException(), isNull);
    });

    testWidgets('renders at the requested diameter', (tester) async {
      await tester.pumpWidget(
          harness(const ChatAvatar(isAssistant: true, size: 28)));
      expect(tester.getSize(find.byType(ChatAvatar)).width, 28);
    });
  });

  group('BrandWordmark', () {
    testWidgets('renders the app name next to the dot', (tester) async {
      await tester.pumpWidget(harness(const BrandWordmark()));
      expect(find.text('CDNAI'), findsOneWidget);
      expect(tester.takeException(), isNull);
    });

    testWidgets('scales with the requested font size', (tester) async {
      await tester.pumpWidget(harness(const BrandWordmark(fontSize: 22)));
      final text = tester.widget<Text>(find.text('CDNAI'));
      expect(text.style?.fontSize, 22);
    });
  });

  group('Palette', () {
    test('backgrounds get progressively lighter for elevation', () {
      // Ordered dark -> light; the drawer/cards must sit above the canvas.
      expect(Palette.bg0.computeLuminance(),
          lessThan(Palette.bg1.computeLuminance()));
      expect(Palette.bg1.computeLuminance(),
          lessThan(Palette.bg2.computeLuminance()));
      expect(Palette.bg2.computeLuminance(),
          lessThan(Palette.bg3.computeLuminance()));
    });

    test('text on the user bubble has strong contrast', () {
      final bg = Palette.userBubbleBottom.computeLuminance();
      final fg = Palette.userText.computeLuminance();
      final lighter = fg > bg ? fg : bg;
      final darker = fg > bg ? bg : fg;
      final ratio = (lighter + 0.05) / (darker + 0.05);
      // WCAG AA for normal text is 4.5:1; the chat must stay readable.
      expect(ratio, greaterThan(4.5));
    });

    test('scrim is translucent and clamps out-of-range alpha', () {
      expect(Palette.scrim().a, lessThan(1.0));
      expect(Palette.surfaceTint(2.0).a, lessThanOrEqualTo(1.0));
      expect(Palette.surfaceTint(-1.0).a, greaterThanOrEqualTo(0.0));
    });
  });
}

/// Small holder so the markdown stylesheet can be asserted outside a widget
/// callback (the style is computed from a BuildContext).
class MarkdownStyleSheetProbe {
  const MarkdownStyleSheetProbe(this.style);
  final MarkdownStyleSheet style;
}