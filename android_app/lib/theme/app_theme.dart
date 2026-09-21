import 'package:flutter/material.dart';
import 'package:flutter_markdown/flutter_markdown.dart';

import 'tokens.dart';

/// Builds the two themes the WebUI ships — the warm "paper" light theme and the
/// warm brown dark theme — from [WebPalette].
///
/// Nothing here invents a colour: every value is a web token, so a change in
/// `webui/src/globals.css` maps to one edit in `theme/tokens.dart`. Buttons,
/// inputs, dialogs, sheets, switches and the markdown sheet are all derived
/// from those tokens so the native client reads as the same product as the web
/// app rather than a lookalike.
class AppTheme {
  const AppTheme._();

  static ThemeData light() => _build(WebPalette.light);
  static ThemeData dark() => _build(WebPalette.dark);

  /// Theme for an explicit [ThemeMode] + platform brightness.
  static ThemeData forMode(ThemeMode mode, Brightness platform) {
    return isDark(mode, platform) ? dark() : light();
  }

  /// Whether [mode] resolves to the dark scheme on a device whose system
  /// setting is [platform] — the same rule `useTheme.ts` applies with
  /// `prefers-color-scheme`.
  static bool isDark(ThemeMode mode, Brightness platform) {
    switch (mode) {
      case ThemeMode.light:
        return false;
      case ThemeMode.dark:
        return true;
      case ThemeMode.system:
        return platform == Brightness.dark;
    }
  }

  /// The token set that [mode] resolves to — what `Palette.activate` needs and
  /// what the theme tests assert on.
  static WebPalette paletteFor(ThemeMode mode, Brightness platform) =>
      isDark(mode, platform) ? WebPalette.dark : WebPalette.light;

  /// Kept for callers that just need "the app theme" (defaults to dark, which
  /// is what the product screenshots and the deployment default to).
  static ThemeData build([Brightness brightness = Brightness.dark]) =>
      brightness == Brightness.dark ? dark() : light();

  static ThemeData _build(WebPalette p) {
    final scheme = ColorScheme(
      brightness: p.brightness,
      primary: p.primary,
      onPrimary: p.primaryForeground,
      primaryContainer: p.secondary,
      onPrimaryContainer: p.foreground,
      secondary: p.secondary,
      onSecondary: p.secondaryForeground,
      secondaryContainer: p.accent,
      onSecondaryContainer: p.accentForeground,
      tertiary: p.highlight,
      onTertiary: p.primaryForeground,
      error: p.destructive,
      onError: p.destructiveForeground,
      surface: p.background,
      onSurface: p.foreground,
      surfaceContainerLowest: p.background,
      surfaceContainerLow: p.card,
      surfaceContainer: p.card,
      surfaceContainerHigh: p.accent,
      surfaceContainerHighest: p.sidebarSelected,
      onSurfaceVariant: p.mutedForeground,
      outline: p.border,
      outlineVariant: p.border,
      shadow: const Color(0xFF000000),
      scrim: const Color(0xFF000000),
      inverseSurface: p.foreground,
      onInverseSurface: p.background,
      inversePrimary: p.primary,
    );

    final base = ThemeData(
      useMaterial3: true,
      brightness: p.brightness,
      colorScheme: scheme,
      scaffoldBackgroundColor: p.background,
      canvasColor: p.card,
      dividerColor: p.border,
      disabledColor: p.mutedForeground.withValues(alpha: 0.5),
      // Web uses the system stack; on Android that is Roboto, so we never ship
      // a bundled font just to look "designed".
      splashFactory: InkRipple.splashFactory,
      extensions: <ThemeExtension<dynamic>>[WebPaletteHolder(p)],
    );

    final controlShape = RoundedRectangleBorder(
      borderRadius: WebRadii.controlAll,
    );

    return base.copyWith(
      pageTransitionsTheme: const PageTransitionsTheme(
        builders: {
          TargetPlatform.android: FadeForwardsPageTransitionsBuilder(),
          TargetPlatform.iOS: CupertinoPageTransitionsBuilder(),
        },
      ),
      textTheme: base.textTheme
          .apply(bodyColor: p.foreground, displayColor: p.foreground)
          .copyWith(
            titleMedium: TextStyle(
              color: p.foreground,
              fontWeight: FontWeight.w500,
              fontSize: WebType.body,
            ),
            bodyMedium: TextStyle(
              color: p.foreground,
              fontSize: WebType.body,
              height: 1.45,
            ),
            bodySmall: TextStyle(
              color: p.mutedForeground,
              fontSize: WebType.rowDescription,
              height: 1.5,
            ),
            labelLarge: TextStyle(
              color: p.foreground,
              fontWeight: FontWeight.w500,
              fontSize: WebType.body,
            ),
          ),
      // Thread header: quiet 12px title on the canvas, exactly like the web
      // `ThreadHeader` (no elevation, no colour block).
      appBarTheme: AppBarTheme(
        backgroundColor: p.background,
        surfaceTintColor: Colors.transparent,
        foregroundColor: p.foreground,
        elevation: 0,
        scrolledUnderElevation: 0,
        centerTitle: false,
        titleSpacing: 0,
        titleTextStyle: TextStyle(
          color: p.mutedForeground,
          fontSize: WebType.headerTitle,
          fontWeight: FontWeight.w500,
        ),
        iconTheme: IconThemeData(color: p.mutedForeground, size: 18),
      ),
      iconTheme: IconThemeData(color: p.mutedForeground, size: 20),
      dividerTheme: DividerThemeData(
        color: p.border.withValues(alpha: 0.45),
        thickness: 1,
        space: 1,
      ),
      drawerTheme: DrawerThemeData(
        backgroundColor: p.sidebar,
        surfaceTintColor: Colors.transparent,
        elevation: 0,
        width: WebSpace.sidebarWidth,
        shape: const RoundedRectangleBorder(),
      ),
      cardTheme: CardThemeData(
        color: p.card,
        surfaceTintColor: Colors.transparent,
        elevation: 0,
        margin: EdgeInsets.zero,
        shape: RoundedRectangleBorder(borderRadius: WebRadii.panelAll),
      ),
      listTileTheme: ListTileThemeData(
        iconColor: p.mutedForeground,
        textColor: p.foreground,
        contentPadding: const EdgeInsets.symmetric(horizontal: 16, vertical: 2),
      ),
      inputDecorationTheme: InputDecorationTheme(
        filled: true,
        fillColor: p.isDark ? p.card : p.background,
        isDense: true,
        hintStyle: TextStyle(
          color: p.mutedForeground.withValues(alpha: 0.8),
          fontSize: 14,
        ),
        labelStyle: TextStyle(color: p.mutedForeground),
        floatingLabelStyle: TextStyle(color: p.foreground),
        contentPadding: const EdgeInsets.symmetric(
          horizontal: 12,
          vertical: 12,
        ),
        border: OutlineInputBorder(
          borderRadius: WebRadii.controlAll,
          borderSide: BorderSide(color: p.input),
        ),
        enabledBorder: OutlineInputBorder(
          borderRadius: WebRadii.controlAll,
          borderSide: BorderSide(color: p.input),
        ),
        focusedBorder: OutlineInputBorder(
          borderRadius: WebRadii.controlAll,
          borderSide: BorderSide(color: p.ring, width: 1.4),
        ),
        errorBorder: OutlineInputBorder(
          borderRadius: WebRadii.controlAll,
          borderSide: BorderSide(color: p.destructive),
        ),
      ),
      // Filled = the web's `variant="default"`: near-black on light, near-white
      // on dark, 40px tall, 12px radius, 14px medium label.
      filledButtonTheme: FilledButtonThemeData(
        style: FilledButton.styleFrom(
          backgroundColor: p.primary,
          foregroundColor: p.primaryForeground,
          disabledBackgroundColor: p.primary.withValues(alpha: 0.5),
          disabledForegroundColor: p.primaryForeground.withValues(alpha: 0.6),
          minimumSize: const Size(0, 40),
          padding: const EdgeInsets.symmetric(horizontal: 16),
          textStyle: const TextStyle(
            fontSize: WebType.body,
            fontWeight: FontWeight.w500,
          ),
          shape: controlShape,
        ),
      ),
      outlinedButtonTheme: OutlinedButtonThemeData(
        style: OutlinedButton.styleFrom(
          foregroundColor: p.foreground,
          backgroundColor: p.background,
          minimumSize: const Size(0, 40),
          padding: const EdgeInsets.symmetric(horizontal: 16),
          side: BorderSide(color: p.input),
          textStyle: const TextStyle(
            fontSize: WebType.body,
            fontWeight: FontWeight.w500,
          ),
          shape: controlShape,
        ),
      ),
      textButtonTheme: TextButtonThemeData(
        style: TextButton.styleFrom(
          foregroundColor: p.foreground,
          minimumSize: const Size(0, 36),
          padding: const EdgeInsets.symmetric(horizontal: 12),
          textStyle: const TextStyle(
            fontSize: WebType.body,
            fontWeight: FontWeight.w500,
          ),
          shape: controlShape,
        ),
      ),
      iconButtonTheme: IconButtonThemeData(
        style: IconButton.styleFrom(
          foregroundColor: p.mutedForeground,
          highlightColor: p.quietFill,
        ),
      ),
      chipTheme: ChipThemeData(
        backgroundColor: p.card,
        selectedColor: p.accent,
        side: BorderSide(color: p.hairline),
        labelStyle: TextStyle(color: p.foreground, fontSize: 12.5),
        shape: RoundedRectangleBorder(borderRadius: WebRadii.pillAll),
      ),
      dialogTheme: DialogThemeData(
        backgroundColor: p.popover,
        surfaceTintColor: Colors.transparent,
        shape: RoundedRectangleBorder(borderRadius: WebRadii.modalAll),
        titleTextStyle: TextStyle(
          color: p.foreground,
          fontSize: WebType.dialogTitle,
          fontWeight: FontWeight.w600,
          letterSpacing: -0.4,
        ),
        contentTextStyle: TextStyle(
          color: p.mutedForeground,
          fontSize: 14,
          height: 1.5,
        ),
      ),
      bottomSheetTheme: BottomSheetThemeData(
        backgroundColor: p.popover,
        surfaceTintColor: Colors.transparent,
        shape: const RoundedRectangleBorder(
          borderRadius: BorderRadius.vertical(
            top: Radius.circular(WebRadii.panel),
          ),
        ),
      ),
      snackBarTheme: SnackBarThemeData(
        backgroundColor: p.foreground,
        contentTextStyle: TextStyle(color: p.background, fontSize: 13),
        behavior: SnackBarBehavior.floating,
        shape: RoundedRectangleBorder(borderRadius: WebRadii.controlAll),
      ),
      popupMenuTheme: PopupMenuThemeData(
        color: p.popover,
        surfaceTintColor: Colors.transparent,
        elevation: 6,
        shape: RoundedRectangleBorder(borderRadius: WebRadii.floatingAll),
        textStyle: TextStyle(color: p.foreground, fontSize: 13.5),
      ),
      tooltipTheme: TooltipThemeData(
        decoration: BoxDecoration(
          color: p.popover,
          borderRadius: WebRadii.floatingAll,
          border: Border.all(color: p.hairline),
        ),
        textStyle: TextStyle(color: p.foreground, fontSize: 12),
      ),
      progressIndicatorTheme: ProgressIndicatorThemeData(
        color: p.primary,
        linearTrackColor: p.accent,
      ),
      // Switches keep the web's single blue control (#2997FF).
      switchTheme: SwitchThemeData(
        thumbColor: WidgetStateProperty.resolveWith(
          (states) => states.contains(WidgetState.selected)
              ? Colors.white
              : p.card,
        ),
        trackColor: WidgetStateProperty.resolveWith(
          (states) => states.contains(WidgetState.selected)
              ? p.toggleOn
              : p.mutedForeground.withValues(alpha: 0.30),
        ),
        trackOutlineColor: const WidgetStatePropertyAll(Colors.transparent),
      ),
      scrollbarTheme: ScrollbarThemeData(
        thumbColor: WidgetStatePropertyAll(
          p.mutedForeground.withValues(alpha: 0.26),
        ),
        thickness: const WidgetStatePropertyAll(4),
        radius: const Radius.circular(4),
      ),
      textSelectionTheme: TextSelectionThemeData(
        cursorColor: p.foreground,
        selectionColor: p.primary.withValues(alpha: 0.15),
        selectionHandleColor: p.primary,
      ),
    );
  }

  /// Markdown styling shared by assistant answers, mirroring the web's
  /// `.markdown-content` prose rules (same --foreground for user and assistant,
  /// 1.625 line-height, code on a muted slab).
  static MarkdownStyleSheet markdown(BuildContext context) {
    final p = WebPalette.of(context);
    return MarkdownStyleSheet.fromTheme(Theme.of(context)).copyWith(
      p: TextStyle(
        color: p.foreground,
        fontSize: WebType.message,
        height: 1.6,
      ),
      h1: TextStyle(
        color: p.foreground,
        fontSize: 21,
        fontWeight: FontWeight.w700,
        height: 1.3,
      ),
      h2: TextStyle(
        color: p.foreground,
        fontSize: 18,
        fontWeight: FontWeight.w600,
        height: 1.3,
      ),
      h3: TextStyle(
        color: p.foreground,
        fontSize: 16,
        fontWeight: FontWeight.w600,
        height: 1.3,
      ),
      strong: TextStyle(color: p.foreground, fontWeight: FontWeight.w600),
      em: TextStyle(color: p.foreground, fontStyle: FontStyle.italic),
      a: TextStyle(
        color: p.foreground,
        decoration: TextDecoration.underline,
        decorationColor: p.mutedForeground,
      ),
      listBullet: TextStyle(
        color: p.foreground,
        fontSize: WebType.message,
        height: 1.6,
      ),
      blockquote: TextStyle(
        color: p.mutedForeground,
        fontSize: 14,
        height: 1.6,
      ),
      blockquoteDecoration: BoxDecoration(
        color: Colors.transparent,
        border: Border(
          left: BorderSide(
            color: p.mutedForeground.withValues(alpha: 0.35),
            width: 3,
          ),
        ),
      ),
      blockquotePadding: const EdgeInsets.only(left: 12),
      code: TextStyle(
        color: p.isDark ? const Color(0xFFF0B27A) : const Color(0xFFB45309),
        fontSize: 13.5,
        backgroundColor: Colors.transparent,
      ),
      codeblockPadding: const EdgeInsets.all(12),
      codeblockDecoration: BoxDecoration(
        color: p.accent,
        borderRadius: BorderRadius.circular(WebRadii.compact),
        border: Border.all(color: p.hairlineSoft),
      ),
      tableBorder: TableBorder.all(color: p.hairlineSoft),
      tableHead: TextStyle(color: p.foreground, fontWeight: FontWeight.w600),
      tableBody: TextStyle(color: p.mutedForeground, fontSize: 13.5),
      horizontalRuleDecoration: BoxDecoration(
        border: Border(top: BorderSide(color: p.hairlineSoft)),
      ),
    );
  }
}
