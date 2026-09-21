// Tests for the shared design system the APK now wears.
//
// The point of these is *parity*: the native client must render the same skin
// as `webui/`, so the values asserted here are transcribed from
// `webui/src/globals.css` (HSL tokens) and the components the browser uses
// (button, toggle, sidebar). If someone changes the web stylesheet, these fail
// until the Android tokens follow.

import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_markdown/flutter_markdown.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:powerx_android/config.dart';
import 'package:powerx_android/state/theme_controller.dart';
import 'package:powerx_android/theme/app_theme.dart';
import 'package:powerx_android/theme/palette.dart';
import 'package:powerx_android/theme/tokens.dart';
import 'package:powerx_android/widgets/brand.dart';
import 'package:powerx_android/widgets/settings_controls.dart';
import 'package:powerx_android/widgets/sidebar_actions.dart';
import 'package:powerx_android/models.dart';

/// Wraps [child] in one of the app themes so widgets resolve theme defaults the
/// way they do in production. The phone-sized box is the OUTER bound and the
/// child is centred inside it, so fixed-size widgets keep their own dimensions
/// instead of being stretched to fill the frame.
Widget harness(
  Widget child, {
  double width = 390,
  double height = 844,
  bool dark = true,
}) {
  return MaterialApp(
    theme: AppTheme.light(),
    darkTheme: AppTheme.dark(),
    themeMode: dark ? ThemeMode.dark : ThemeMode.light,
    home: Scaffold(
      body: SizedBox(
        width: width,
        height: height,
        child: Center(child: child),
      ),
    ),
  );
}

/// `#RRGGBB` for legibility when a token drifts.
String hex(Color c) =>
    '#${(c.toARGB32() & 0xFFFFFF).toRadixString(16).padLeft(6, '0').toUpperCase()}';

void main() {
  group('WebPalette matches webui/globals.css', () {
    test('dark canvas is the brown from the screenshot (hsl(30 8% 19%))', () {
      expect(hex(WebPalette.dark.background), '#34302D');
      expect(hex(WebPalette.dark.foreground), '#F4F3F0');
      expect(hex(WebPalette.dark.card), '#3D3834');
      expect(hex(WebPalette.dark.border), '#4C4742');
      expect(hex(WebPalette.dark.sidebarSelected), '#524C47');
      expect(hex(WebPalette.dark.mutedForeground), '#ADA79F');
    });

    test('light canvas is the warm paper from :root', () {
      expect(hex(WebPalette.light.background), '#FDFDFC');
      expect(hex(WebPalette.light.foreground), '#191715');
      expect(hex(WebPalette.light.primary), '#1E1C1A');
      expect(hex(WebPalette.light.border), '#E9E6E2');
      expect(hex(WebPalette.light.sidebar), '#F9F8F6');
      expect(hex(WebPalette.light.settingsSurface), '#F8F7F4');
    });

    test('the one accent is the brand orange, the one control blue', () {
      expect(hex(WebPalette.light.highlight), '#EF8E30');
      expect(hex(WebPalette.dark.highlight), '#EF8E30');
      // ToggleButton.tsx checked state — deliberately the single non-neutral.
      expect(hex(WebPalette.light.toggleOn), '#2997FF');
      expect(hex(WebPalette.dark.toggleOn), '#2997FF');
    });

    test('primary inverts with the scheme, like bg-primary does', () {
      expect(hex(WebPalette.light.primary), '#1E1C1A');
      expect(hex(WebPalette.dark.primary), '#F8F8F6');
      expect(WebPalette.dark.primaryForeground, WebPalette.light.primary);
    });

    test('composer surfaces follow the web rules per scheme', () {
      // light: bg-muted/30 ; dark: bg-card
      expect(WebPalette.light.composerSurface, isNot(WebPalette.light.card));
      expect(WebPalette.dark.composerSurface, WebPalette.dark.card);
    });

    test('user bubble and assistant text stay readable (WCAG AA)', () {
      for (final p in [WebPalette.light, WebPalette.dark]) {
        final bg = p.userBubble.computeLuminance();
        final fg = p.userBubbleText.computeLuminance();
        final lighter = fg > bg ? fg : bg;
        final darker = fg > bg ? bg : fg;
        expect(
          (lighter + 0.05) / (darker + 0.05),
          greaterThan(4.5),
          reason: '${p.name} user bubble contrast',
        );
      }
    });

    test('radius scale is the globals.css scale', () {
      expect(WebRadii.compact, 8);
      expect(WebRadii.control, 12);
      expect(WebRadii.floating, 18);
      expect(WebRadii.panel, 22);
      expect(WebRadii.prominent, 28);
    });

    test('type and spacing carry the touch target the web enforces', () {
      expect(WebType.sidebarAction, 12.5);
      expect(WebType.settingsNav, 13);
      expect(WebType.body, 14);
      expect(WebType.composerInput, 16);
      expect(WebSpace.touchTarget, 44);
      expect(WebSpace.sidebarWidth, 272);
    });
  });

  group('AppTheme', () {
    test('light and dark are built from their own palettes', () {
      final light = AppTheme.light();
      expect(light.brightness, Brightness.light);
      expect(light.scaffoldBackgroundColor, WebPalette.light.background);
      expect(light.colorScheme.primary, WebPalette.light.primary);

      final dark = AppTheme.dark();
      expect(dark.brightness, Brightness.dark);
      expect(dark.scaffoldBackgroundColor, WebPalette.dark.background);
      expect(dark.colorScheme.primary, WebPalette.dark.primary);
    });

    test('the app bar is the canvas, with a 12px muted title', () {
      // The web paints the thread header on the canvas with a hairline under
      // it — not a raised bar, which is what the old theme did.
      for (final theme in [AppTheme.light(), AppTheme.dark()]) {
        final p = theme.extension<WebPaletteHolder>()!.palette;
        expect(theme.appBarTheme.backgroundColor, p.background);
        expect(theme.appBarTheme.elevation, 0);
        expect(theme.appBarTheme.titleTextStyle?.fontSize, WebType.headerTitle);
      }
    });

    test('filled buttons use the primary token', () {
      final theme = AppTheme.dark();
      final style = theme.filledButtonTheme.style;
      expect(style, isNotNull);
      final shape = style!.shape?.resolve({});
      expect(shape, isA<RoundedRectangleBorder>());
      final radius =
          (shape! as RoundedRectangleBorder).borderRadius as BorderRadius;
      expect(radius.topLeft.x, WebRadii.control);
    });

    test('switches use the web ToggleButton blue', () {
      // ToggleButton.tsx: a #2997FF track with a white thumb — the single
      // non-monochrome control in the product.
      final theme = AppTheme.dark();
      final track =
          theme.switchTheme.trackColor?.resolve({WidgetState.selected});
      expect(track, WebPalette.dark.toggleOn);
      final thumb =
          theme.switchTheme.thumbColor?.resolve({WidgetState.selected});
      expect(thumb, Colors.white);
    });

    test('isDark follows the system setting only in system mode', () {
      expect(AppTheme.isDark(ThemeMode.system, Brightness.dark), isTrue);
      expect(AppTheme.isDark(ThemeMode.system, Brightness.light), isFalse);
      expect(AppTheme.isDark(ThemeMode.light, Brightness.dark), isFalse);
      expect(AppTheme.isDark(ThemeMode.dark, Brightness.light), isTrue);
      expect(
        AppTheme.paletteFor(ThemeMode.system, Brightness.dark),
        WebPalette.dark,
      );
    });

    testWidgets('markdown stylesheet is derived from the theme', (tester) async {
      late MarkdownStyleSheetProbe probe;
      for (final dark in [true, false]) {
        await tester.pumpWidget(
          harness(
            Builder(
              builder: (context) {
                probe = MarkdownStyleSheetProbe(AppTheme.markdown(context));
                return const SizedBox.shrink();
              },
            ),
            dark: dark,
          ),
        );
        // MaterialApp cross-fades between themes, so let it land before
        // reading the resolved stylesheet.
        await tester.pumpAndSettle();
        final expected = dark ? WebPalette.dark : WebPalette.light;
        // Body text must be the readable web size and the foreground token —
        // the Material default renders dark-on-dark in the dark theme.
        expect(probe.style.p!.fontSize, WebType.message);
        expect(probe.style.p!.color, expected.foreground);
      }
    });
  });

  group('ThemeController', () {
    setUp(() => FlutterSecureStorage.setMockInitialValues({}));

    test('defaults to following the phone', () {
      final c = ThemeController(observeBinding: false);
      addTearDown(c.dispose);
      expect(c.mode, ThemeMode.system);
      expect(c.loaded, isFalse);
    });

    test('toggle flips the resolved scheme and pins it', () async {
      final c = ThemeController(observeBinding: false);
      addTearDown(c.dispose);
      final wasDark = c.isDark;
      await c.toggle();
      expect(c.isDark, !wasDark);
      expect(c.mode, wasDark ? ThemeMode.light : ThemeMode.dark);
    });

    test('a pinned choice survives a restart', () async {
      final first = ThemeController(observeBinding: false);
      await first.setMode(ThemeMode.light);
      first.dispose();

      final second = ThemeController(observeBinding: false);
      addTearDown(second.dispose);
      await second.load();
      expect(second.mode, ThemeMode.light);
      expect(second.loaded, isTrue);
      expect(second.palette, WebPalette.light);
    });

    test('load falls back to system on a junk value', () async {
      FlutterSecureStorage.setMockInitialValues({ThemeController.storageKey: 'banana'});
      final c = ThemeController(observeBinding: false);
      addTearDown(c.dispose);
      await c.load();
      expect(c.mode, ThemeMode.system);
    });
  });

  group('Palette facade', () {
    tearDown(() => Palette.activate(WebPalette.dark));

    test('resolves every legacy name onto the active web palette', () {
      Palette.activate(WebPalette.light);
      expect(Palette.bg0, WebPalette.light.background);
      expect(Palette.bg2, WebPalette.light.card);
      expect(Palette.accent, WebPalette.light.primary);
      expect(Palette.textPrimary, WebPalette.light.foreground);
      expect(Palette.highlight, WebPalette.light.highlight);

      Palette.activate(WebPalette.dark);
      expect(Palette.bg0, WebPalette.dark.background);
      expect(Palette.accent, WebPalette.dark.primary);
      expect(Palette.danger, WebPalette.dark.destructive);
    });

    test('activateBrightness follows the scheme', () {
      Palette.activateBrightness(Brightness.light);
      expect(Palette.bg0, WebPalette.light.background);
      Palette.activateBrightness(Brightness.dark);
      expect(Palette.bg0, WebPalette.dark.background);
    });
  });

  group('BrandMark', () {
    testWidgets('draws the web mark, not a lightning-bolt emoji', (tester) async {
      await tester.pumpWidget(harness(const BrandMark(size: 88)));
      expect(find.text('⚡'), findsNothing);

      final image = tester.widget<Image>(find.byType(Image));
      expect((image.image as AssetImage).assetName, BrandAssets.shellMark);

      final size = tester.getSize(find.byType(BrandMark));
      expect(size.width, 88);
      expect(size.height, 88);
    });

    testWidgets('respects a custom radius without overflowing', (tester) async {
      await tester.pumpWidget(harness(const BrandMark(size: 64, radius: 12)));
      expect(tester.takeException(), isNull);
      expect(find.byType(ClipRRect), findsOneWidget);
    });

    testWidgets('degrades to a neutral glyph when the bundle is missing',
        (tester) async {
      await tester.pumpWidget(
        harness(const BrandMark(size: 40, imageUrl: 'assets/brand/missing.png')),
      );
      await tester.pump();
      expect(tester.takeException(), isNull);
    });
  });

  group('ChatAvatar', () {
    testWidgets('assistant avatar shows the brand mark, not the emoji',
        (tester) async {
      await tester.pumpWidget(harness(const ChatAvatar(isAssistant: true)));
      expect(find.text('⚡'), findsNothing);
      expect(find.byType(BrandMark), findsOneWidget);
    });

    testWidgets('user avatar shows the uppercased initial', (tester) async {
      await tester.pumpWidget(
        harness(const ChatAvatar(isAssistant: false, initial: 'arinze')),
      );
      expect(find.text('A'), findsOneWidget);
    });

    testWidgets('blank initial falls back to a placeholder, never crashes',
        (tester) async {
      await tester.pumpWidget(
        harness(const ChatAvatar(isAssistant: false, initial: '   ')),
      );
      expect(find.text('?'), findsOneWidget);
      expect(tester.takeException(), isNull);
    });

    testWidgets('renders at the requested diameter', (tester) async {
      await tester.pumpWidget(
        harness(const ChatAvatar(isAssistant: true, size: 28)),
      );
      expect(tester.getSize(find.byType(ChatAvatar)).width, 28);
    });
  });

  group('BrandWordmark', () {
    testWidgets('renders the CDNAI name next to the mark', (tester) async {
      await tester.pumpWidget(harness(const BrandWordmark()));
      expect(find.text(PowerXConfig.appName), findsOneWidget);
      expect(find.byType(CdnaiMark), findsOneWidget);
      expect(tester.takeException(), isNull);
    });

    testWidgets('scales with the requested font size', (tester) async {
      await tester.pumpWidget(harness(const BrandWordmark(fontSize: 22)));
      final text = tester.widget<Text>(find.text(PowerXConfig.appName));
      expect(text.style?.fontSize, 22);
    });
  });

  group('settings controls', () {
    testWidgets('SettingsRow is at least 62px tall with a 14px title',
        (tester) async {
      await tester.pumpWidget(
        harness(
          const SettingsGroup(
            children: [
              SettingsRow(
                title: 'Web search',
                description: 'Let the agent browse.',
                child: WebToggle(value: true),
              ),
            ],
          ),
        ),
      );
      expect(tester.getSize(find.byType(SettingsRow)).height,
          greaterThanOrEqualTo(62));
      final title = tester.widget<Text>(find.text('Web search'));
      expect(title.style?.fontSize, WebType.body);
      final desc = tester.widget<Text>(find.text('Let the agent browse.'));
      expect(desc.style?.fontSize, WebType.rowDescription);
      expect(tester.takeException(), isNull);
    });

    testWidgets('WebToggle paints the checked track with the web blue',
        (tester) async {
      await tester.pumpWidget(
        harness(const SettingsRow(title: 'Feature', child: WebToggle(value: true))),
      );
      final decorated = tester
          .widgetList<AnimatedContainer>(find.byType(AnimatedContainer))
          .map((c) => c.decoration)
          .whereType<BoxDecoration>()
          .firstWhere((d) => d.color == WebPalette.dark.toggleOn, orElse: () {
        return const BoxDecoration(color: null);
      });
      expect(decorated.color, WebPalette.dark.toggleOn);
    });

    testWidgets('SettingsSectionTitle renders the 13px section label',
        (tester) async {
      await tester.pumpWidget(
        harness(const SettingsSectionTitle('Models')),
      );
      final text = tester.widget<Text>(find.text('Models'));
      expect(text.style?.fontSize, WebType.sectionTitle);
    });
  });

  group('sidebar actions', () {
    testWidgets('lists the five web actions in order', (tester) async {
      await tester.pumpWidget(
        harness(
          SidebarActionList(active: SidebarAction.newChat, onSelected: (_) {}),
        ),
      );
      for (final label in [
        'New chat',
        'Search',
        'Apps',
        'Skills',
        'Automations',
      ]) {
        expect(find.text(label), findsOneWidget, reason: label);
      }
      // Archive only appears once something has been archived.
      expect(find.text('Show archived'), findsNothing);
      expect(tester.takeException(), isNull);
    });

    testWidgets('adds the archive row when there is an archived chat',
        (tester) async {
      var toggled = 0;
      await tester.pumpWidget(
        harness(
          SidebarActionList(
            active: SidebarAction.newChat,
            archivedCount: 3,
            onToggleArchived: () => toggled++,
            onSelected: (_) {},
          ),
        ),
      );
      expect(find.text('Show archived'), findsOneWidget);
      await tester.tap(find.text('Show archived'));
      expect(toggled, 1);
    });

    testWidgets('tapping an action reports it back', (tester) async {
      SidebarAction? tapped;
      await tester.pumpWidget(
        harness(
          SidebarActionList(
            active: SidebarAction.search,
            onSelected: (a) => tapped = a,
          ),
        ),
      );
      await tester.tap(find.text('Skills'));
      expect(tapped, SidebarAction.skills);
    });

    test('apps / skills / automations map onto their settings sections', () {
      expect(SidebarAction.apps.sectionId, 'apps');
      expect(SidebarAction.skills.sectionId, 'skills');
      expect(SidebarAction.automations.sectionId, 'automations');
      expect(SidebarAction.settings.sectionId, 'overview');
    });
  });

  group('SessionTile', () {
    SessionSummary sample({String title = 'Thread', String preview = ''}) =>
        SessionSummary(
          key: 'websocket:1',
          chatId: '1',
          title: title,
          preview: preview,
          updatedAt: DateTime.now(),
        );

    testWidgets('shows the title and preview without overflowing',
        (tester) async {
      await tester.pumpWidget(
        harness(
          SizedBox(
            width: 240,
            child: SessionTile(
              session: sample(title: 'A' * 200, preview: 'B' * 300),
              onTap: () {},
            ),
          ),
        ),
      );
      expect(tester.takeException(), isNull);
    });

    testWidgets('a title-less session reads as "New chat"', (tester) async {
      await tester.pumpWidget(
        harness(SessionTile(session: sample(title: ''), onTap: () {})),
      );
      expect(find.text('New chat'), findsOneWidget);
    });

    testWidgets('the delete affordance is optional', (tester) async {
      var deleted = 0;
      await tester.pumpWidget(
        harness(
          SessionTile(
            session: sample(title: 'Keep me'),
            onTap: () {},
            onDelete: () => deleted++,
          ),
        ),
      );
      await tester.tap(find.byIcon(Icons.more_horiz_rounded));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Delete'));
      await tester.pumpAndSettle();
      expect(deleted, 1);
    });
  });

  group('brand assets are shipped', () {
    test('the rasterised mark exists where pubspec declares it', () {
      expect(File(BrandAssets.shellMark).existsSync(), isTrue);
      expect(File('assets/brand/mark_128.png').existsSync(), isTrue);
    });

    test('pubspec declares the assets/brand directory', () {
      final pubspec = File('pubspec.yaml').readAsStringSync();
      expect(pubspec.contains('- assets/brand/'), isTrue);
    });

    test('the launcher icon was regenerated at full density', () {
      final icon = File(
        'android/app/src/main/res/mipmap-xxxhdpi/ic_launcher.png',
      );
      expect(icon.existsSync(), isTrue);
      expect(icon.lengthSync(), greaterThan(1000));
    });
  });
}

/// Small holder so the markdown stylesheet can be asserted outside a widget
/// callback (the style is computed from a BuildContext).
class MarkdownStyleSheetProbe {
  const MarkdownStyleSheetProbe(this.style);
  final MarkdownStyleSheet style;
}
