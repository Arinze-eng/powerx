import 'package:flutter/material.dart';
import 'package:flutter_markdown/flutter_markdown.dart';

import 'palette.dart';

/// Builds the single dark "coffee" theme used across the app.
///
/// Everything visual — colour scheme, typography, app bar, inputs, buttons,
/// dialogs, drawers — is derived here so individual widgets stay lean and
/// consistent.
class AppTheme {
  const AppTheme._();

  static ThemeData build() {
    const scheme = ColorScheme(
      brightness: Brightness.dark,
      primary: Palette.accent,
      onPrimary: Color(0xFF241407),
      primaryContainer: Palette.accentDeep,
      onPrimaryContainer: Palette.textPrimary,
      secondary: Palette.accentSoft,
      onSecondary: Color(0xFF241407),
      secondaryContainer: Palette.bg3,
      onSecondaryContainer: Palette.textPrimary,
      tertiary: Palette.warning,
      onTertiary: Color(0xFF241407),
      error: Palette.danger,
      onError: Color(0xFF2A0D08),
      surface: Palette.bg0,
      onSurface: Palette.textPrimary,
      surfaceContainerLowest: Palette.bg0,
      surfaceContainerLow: Palette.bg1,
      surfaceContainer: Palette.bg2,
      surfaceContainerHigh: Palette.bg3,
      surfaceContainerHighest: Palette.bg4,
      onSurfaceVariant: Palette.textSecondary,
      outline: Palette.border,
      outlineVariant: Palette.borderSoft,
      shadow: Color(0xFF000000),
      scrim: Color(0xFF000000),
      inverseSurface: Palette.textPrimary,
      onInverseSurface: Palette.bg0,
      inversePrimary: Palette.accentDeep,
    );

    final base = ThemeData(
      useMaterial3: true,
      brightness: Brightness.dark,
      colorScheme: scheme,
      scaffoldBackgroundColor: Palette.bg0,
      canvasColor: Palette.bg1,
      dividerColor: Palette.borderSoft,
      fontFamily: 'Roboto',
      splashFactory: InkSparkle.splashFactory,
    );

    return base.copyWith(
      appBarTheme: const AppBarTheme(
        backgroundColor: Palette.bg1,
        surfaceTintColor: Colors.transparent,
        foregroundColor: Palette.textPrimary,
        elevation: 0,
        scrolledUnderElevation: 0,
        centerTitle: true,
        titleTextStyle: TextStyle(
          color: Palette.textPrimary,
          fontSize: 17,
          fontWeight: FontWeight.w700,
          letterSpacing: 0.2,
        ),
      ),
      textTheme: base.textTheme
          .apply(
            bodyColor: Palette.textPrimary,
            displayColor: Palette.textPrimary,
          )
          .copyWith(
            titleMedium: const TextStyle(
              color: Palette.textPrimary,
              fontWeight: FontWeight.w700,
              fontSize: 16,
            ),
            bodyMedium: const TextStyle(
              color: Palette.textPrimary,
              fontSize: 14.5,
              height: 1.4,
            ),
            labelLarge: const TextStyle(
              color: Palette.textPrimary,
              fontWeight: FontWeight.w600,
              fontSize: 14,
            ),
          ),
      iconTheme: const IconThemeData(color: Palette.textSecondary, size: 22),
      drawerTheme: const DrawerThemeData(
        backgroundColor: Palette.bg1,
        surfaceTintColor: Colors.transparent,
        elevation: 0,
        width: 312,
      ),
      dividerTheme: const DividerThemeData(
        color: Palette.borderSoft,
        thickness: 1,
        space: 1,
      ),
      cardTheme: const CardThemeData(
        color: Palette.bg2,
        surfaceTintColor: Colors.transparent,
        elevation: 0,
        margin: EdgeInsets.zero,
      ),
      listTileTheme: const ListTileThemeData(
        iconColor: Palette.textTertiary,
        textColor: Palette.textPrimary,
        contentPadding: EdgeInsets.symmetric(horizontal: 14, vertical: 2),
      ),
      inputDecorationTheme: InputDecorationTheme(
        filled: true,
        fillColor: Palette.bg3,
        isDense: true,
        hintStyle: const TextStyle(color: Palette.textTertiary, fontSize: 14.5),
        labelStyle: const TextStyle(color: Palette.textSecondary),
        floatingLabelStyle: const TextStyle(color: Palette.accentSoft),
        contentPadding: const EdgeInsets.symmetric(
          horizontal: 14,
          vertical: 14,
        ),
        border: OutlineInputBorder(
          borderRadius: BorderRadius.circular(14),
          borderSide: const BorderSide(color: Palette.border),
        ),
        enabledBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(14),
          borderSide: const BorderSide(color: Palette.border),
        ),
        focusedBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(14),
          borderSide: const BorderSide(color: Palette.accent, width: 1.4),
        ),
        errorBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(14),
          borderSide: const BorderSide(color: Palette.danger),
        ),
      ),
      filledButtonTheme: FilledButtonThemeData(
        style: FilledButton.styleFrom(
          backgroundColor: Palette.accent,
          foregroundColor: const Color(0xFF241407),
          disabledBackgroundColor: Palette.bg3,
          disabledForegroundColor: Palette.textTertiary,
          textStyle: const TextStyle(fontWeight: FontWeight.w700, fontSize: 15),
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(14),
          ),
        ),
      ),
      outlinedButtonTheme: OutlinedButtonThemeData(
        style: OutlinedButton.styleFrom(
          foregroundColor: Palette.accentSoft,
          side: const BorderSide(color: Palette.border),
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(14),
          ),
        ),
      ),
      textButtonTheme: TextButtonThemeData(
        style: TextButton.styleFrom(foregroundColor: Palette.accentSoft),
      ),
      iconButtonTheme: IconButtonThemeData(
        style: IconButton.styleFrom(
          foregroundColor: Palette.textSecondary,
          highlightColor: Palette.accentWash,
        ),
      ),
      floatingActionButtonTheme: const FloatingActionButtonThemeData(
        backgroundColor: Palette.accent,
        foregroundColor: Color(0xFF241407),
      ),
      chipTheme: ChipThemeData(
        backgroundColor: Palette.bg3,
        selectedColor: Palette.accentWash,
        side: const BorderSide(color: Palette.border),
        labelStyle: const TextStyle(
          color: Palette.textSecondary,
          fontSize: 12.5,
        ),
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(10)),
      ),
      dialogTheme: DialogThemeData(
        backgroundColor: Palette.bg2,
        surfaceTintColor: Colors.transparent,
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(18)),
        titleTextStyle: const TextStyle(
          color: Palette.textPrimary,
          fontSize: 17,
          fontWeight: FontWeight.w700,
        ),
        contentTextStyle: const TextStyle(
          color: Palette.textSecondary,
          fontSize: 14,
          height: 1.4,
        ),
      ),
      bottomSheetTheme: const BottomSheetThemeData(
        backgroundColor: Palette.bg2,
        surfaceTintColor: Colors.transparent,
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.vertical(top: Radius.circular(22)),
        ),
      ),
      snackBarTheme: SnackBarThemeData(
        backgroundColor: Palette.bg4,
        contentTextStyle: const TextStyle(color: Palette.textPrimary),
        behavior: SnackBarBehavior.floating,
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
      ),
      popupMenuTheme: PopupMenuThemeData(
        color: Palette.bg3,
        surfaceTintColor: Colors.transparent,
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
        textStyle: const TextStyle(color: Palette.textPrimary, fontSize: 14),
      ),
      tooltipTheme: TooltipThemeData(
        decoration: BoxDecoration(
          color: Palette.bg4,
          borderRadius: BorderRadius.circular(8),
        ),
        textStyle: const TextStyle(color: Palette.textPrimary, fontSize: 12),
      ),
      progressIndicatorTheme: const ProgressIndicatorThemeData(
        color: Palette.accent,
        linearTrackColor: Palette.bg3,
      ),
      switchTheme: SwitchThemeData(
        thumbColor: WidgetStateProperty.resolveWith(
          (states) =>
              states.contains(WidgetState.selected)
                  ? Palette.accent
                  : Palette.textTertiary,
        ),
        trackColor: WidgetStateProperty.resolveWith(
          (states) =>
              states.contains(WidgetState.selected)
                  ? Palette.accentWash
                  : Palette.bg3,
        ),
      ),
      scrollbarTheme: ScrollbarThemeData(
        thumbColor: WidgetStatePropertyAll(
          Palette.textTertiary.withValues(alpha: 0.35),
        ),
        thickness: const WidgetStatePropertyAll(3),
        radius: const Radius.circular(3),
      ),
      textSelectionTheme: const TextSelectionThemeData(
        cursorColor: Palette.accent,
        selectionColor: Palette.accentWash,
        selectionHandleColor: Palette.accent,
      ),
    );
  }

  /// Markdown styling shared by assistant answers.
  static MarkdownStyleSheet markdown(BuildContext context) {
    return MarkdownStyleSheet.fromTheme(Theme.of(context)).copyWith(
      p: const TextStyle(
        color: Palette.textPrimary,
        fontSize: 15,
        height: 1.45,
      ),
      h1: const TextStyle(
        color: Palette.textPrimary,
        fontSize: 21,
        fontWeight: FontWeight.w800,
      ),
      h2: const TextStyle(
        color: Palette.textPrimary,
        fontSize: 18,
        fontWeight: FontWeight.w700,
      ),
      h3: const TextStyle(
        color: Palette.textPrimary,
        fontSize: 16,
        fontWeight: FontWeight.w700,
      ),
      strong: const TextStyle(
        color: Palette.textPrimary,
        fontWeight: FontWeight.w700,
      ),
      em: const TextStyle(
        color: Palette.textSecondary,
        fontStyle: FontStyle.italic,
      ),
      a: const TextStyle(
        color: Palette.accentSoft,
        decoration: TextDecoration.underline,
      ),
      listBullet: const TextStyle(
        color: Palette.textPrimary,
        fontSize: 15,
        height: 1.45,
      ),
      blockquote: const TextStyle(
        color: Palette.textSecondary,
        fontSize: 14.5,
        height: 1.4,
      ),
      blockquoteDecoration: const BoxDecoration(
        color: Palette.bg3,
        borderRadius: BorderRadius.all(Radius.circular(8)),
        border: Border(left: BorderSide(color: Palette.accent, width: 3)),
      ),
      code: const TextStyle(
        color: Palette.accentSoft,
        fontSize: 13.5,
        backgroundColor: Colors.transparent,
      ),
      codeblockPadding: const EdgeInsets.all(12),
      codeblockDecoration: BoxDecoration(
        color: Palette.scrim(0.55),
        borderRadius: BorderRadius.circular(10),
        border: Border.all(color: Palette.borderSoft),
      ),
      tableBorder: TableBorder.all(color: Palette.border, width: 1),
      tableHead: const TextStyle(
        color: Palette.textPrimary,
        fontWeight: FontWeight.w700,
      ),
      tableBody: const TextStyle(color: Palette.textSecondary, fontSize: 13.5),
      horizontalRuleDecoration: const BoxDecoration(
        border: Border(top: BorderSide(color: Palette.border)),
      ),
    );
  }
}
