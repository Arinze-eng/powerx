import 'package:flutter/material.dart';

/// Design tokens mirrored 1:1 from the WebUI (`webui/src/globals.css` and
/// `webui/tailwind.config.js`).
///
/// The Android client is a native client of the same product, so it must wear
/// the same skin: a warm off-white "paper" canvas in light mode and a warm
/// brown canvas in dark mode, near-black / near-white primary actions, warm
/// hairline borders and a single orange highlight used only for meaning
/// (temporary chats, inline tokens).
///
/// Every value here is converted from the CSS `hsl()` token, not eyeballed —
/// `tools/gen_tokens.py` regenerates these constants from the stylesheet.
@immutable
class WebPalette {
  const WebPalette({
    required this.brightness,
    required this.name,
    required this.background,
    required this.foreground,
    required this.card,
    required this.popover,
    required this.primary,
    required this.primaryForeground,
    required this.secondary,
    required this.secondaryForeground,
    required this.muted,
    required this.mutedForeground,
    required this.accent,
    required this.accentForeground,
    required this.destructive,
    required this.destructiveForeground,
    required this.border,
    required this.input,
    required this.ring,
    required this.sidebar,
    required this.sidebarForeground,
    required this.sidebarSelected,
    required this.sidebarAccent,
    required this.sidebarAccentForeground,
    required this.sidebarBorder,
    required this.settingsCanvas,
    required this.settingsSurface,
    required this.highlight,
    required this.temporaryAccent,
    required this.temporaryForeground,
    required this.temporaryBorder,
    required this.toggleOn,
    required this.success,
    required this.warning,
    required this.warningForeground,
  });

  final Brightness brightness;

  /// "light" | "dark" — used by tests and debug surfaces.
  final String name;

  // ---- Core surfaces ----------------------------------------------------
  final Color background;
  final Color foreground;
  final Color card;
  final Color popover;
  final Color primary;
  final Color primaryForeground;
  final Color secondary;
  final Color secondaryForeground;
  final Color muted;
  final Color mutedForeground;
  final Color accent;
  final Color accentForeground;
  final Color destructive;
  final Color destructiveForeground;
  final Color border;
  final Color input;
  final Color ring;

  // ---- Sidebar ----------------------------------------------------------
  final Color sidebar;
  final Color sidebarForeground;
  final Color sidebarSelected;
  final Color sidebarAccent;
  final Color sidebarAccentForeground;
  final Color sidebarBorder;

  // ---- Settings ---------------------------------------------------------
  final Color settingsCanvas;
  final Color settingsSurface;

  /// `--inline-token-highlight` / `--temporary-control-active` (#ef8e30).
  final Color highlight;
  final Color temporaryAccent;
  final Color temporaryForeground;
  final Color temporaryBorder;

  /// The one non-monochrome control in the app: switches use #2997FF, exactly
  /// like the web `ToggleButton`.
  final Color toggleOn;

  // ---- Semantic ---------------------------------------------------------
  final Color success;
  final Color warning;
  final Color warningForeground;

  bool get isDark => brightness == Brightness.dark;

  /// Read the palette that the active theme carries.
  static WebPalette of(BuildContext context) =>
      Theme.of(context).extension<WebPaletteHolder>()?.palette ?? light;

  // ---- Light: hsl tokens from `:root` ----------------------------------
  static const light = WebPalette(
    brightness: Brightness.light,
    name: 'light',
    background: Color(0xFFFDFDFC), // 40 20% 99%
    foreground: Color(0xFF191715), // 30 8% 9%
    card: Color(0xFFFFFFFF), // 0 0% 100%
    popover: Color(0xFFFFFFFF),
    primary: Color(0xFF1E1C1A), // 30 8% 11%
    primaryForeground: Color(0xFFFDFDFC), // 40 20% 99%
    secondary: Color(0xFFF5F4F2), // 38 16% 95.5%
    secondaryForeground: Color(0xFF1E1C1A),
    muted: Color(0xFFF5F4F2),
    mutedForeground: Color(0xFF7A736C), // 33 6% 45%
    accent: Color(0xFFF5F4F2),
    accentForeground: Color(0xFF1E1C1A),
    destructive: Color(0xFFEF4444), // 0 84.2% 60.2%
    destructiveForeground: Color(0xFFFAFAFA),
    border: Color(0xFFE9E6E2), // 36 14% 90%
    input: Color(0xFFE9E6E2),
    ring: Color(0xFF1E1C1A),
    sidebar: Color(0xFFF9F8F6), // 38 18% 97%
    sidebarForeground: Color(0xFF1E1C1A),
    sidebarSelected: Color(0xFFEBE9E5), // 36 14% 91%
    sidebarAccent: Color(0xFFF5F4F2),
    sidebarAccentForeground: Color(0xFF1E1C1A),
    sidebarBorder: Color(0xFFE9E6E2),
    settingsCanvas: Color(0xFFFDFDFC),
    settingsSurface: Color(0xFFF8F7F4), // 38 18% 96.5%
    highlight: Color(0xFFEF8E30),
    temporaryAccent: Color(0xFFF97015), // 24 95% 53%
    temporaryForeground: Color(0xFF99320A), // 17 88% 32%
    temporaryBorder: Color(0xFFC03F0C), // 17 88% 40%
    toggleOn: Color(0xFF2997FF),
    success: Color(0xFF047857), // emerald-700 (web text-emerald-700)
    warning: Color(0xFFB45309), // amber-700
    warningForeground: Color(0xFF92400E), // amber-800
  );

  // ---- Dark: hsl tokens from `.dark` -----------------------------------
  static const dark = WebPalette(
    brightness: Brightness.dark,
    name: 'dark',
    background: Color(0xFF34302D), // 30 8% 19%
    foreground: Color(0xFFF4F3F0), // 38 14% 95%
    card: Color(0xFF3D3834), // 30 8% 22%
    popover: Color(0xFF3D3834),
    primary: Color(0xFFF8F8F6), // 38 14% 97%
    primaryForeground: Color(0xFF1E1C1A), // 30 8% 11%
    secondary: Color(0xFF3D3834),
    secondaryForeground: Color(0xFFF6F5F3), // 38 14% 96%
    muted: Color(0xFF3D3834),
    mutedForeground: Color(0xFFADA79F), // 36 8% 65%
    accent: Color(0xFF44403B), // 30 7% 25%
    accentForeground: Color(0xFFF6F5F3),
    destructive: Color(0xFF7F1D1D), // 0 62.8% 30.6%
    destructiveForeground: Color(0xFFFAFAFA),
    border: Color(0xFF4C4742), // 30 7% 28%
    input: Color(0xFF4C4742),
    ring: Color(0xFFD8D5CF), // 36 10% 83%
    sidebar: Color(0xFF3D3834),
    sidebarForeground: Color(0xFFF6F5F3),
    sidebarSelected: Color(0xFF524C47), // 30 7% 30%
    sidebarAccent: Color(0xFF34302D),
    sidebarAccentForeground: Color(0xFFF6F5F3),
    sidebarBorder: Color(0xFF4C4742),
    settingsCanvas: Color(0xFF34302D),
    settingsSurface: Color(0xFF3D3834),
    highlight: Color(0xFFEF8E30),
    temporaryAccent: Color(0xFFF97015),
    temporaryForeground: Color(0xFFFEBF77), // 32 98% 73%
    temporaryBorder: Color(0xFFFB923C), // 27 96% 61%
    toggleOn: Color(0xFF2997FF),
    success: Color(0xFF34D399), // emerald-400 (web dark:text-emerald-400)
    warning: Color(0xFFFCD34D), // amber-300
    warningForeground: Color(0xFFFDE68A), // amber-200
  );

  // ---- Helpers used across the client ----------------------------------

  /// `bg-muted/30` in light, `bg-card` in dark — the composer surface.
  Color get composerSurface =>
      isDark ? card : muted.withValues(alpha: 0.30);

  /// `bg-muted/50` — the focused composer surface.
  Color get composerSurfaceFocused =>
      isDark ? Color.alphaBlend(Colors.white.withValues(alpha: 0.06), card) : muted.withValues(alpha: 0.50);

  /// Hairline used by rows and cards (`border/55`, `border-border/45`).
  Color get hairline => border.withValues(alpha: 0.55);
  Color get hairlineSoft => border.withValues(alpha: 0.45);

  /// The translucent row highlight behind the active sidebar/settings item
  /// (`bg-sidebar-foreground/[0.055]` / `dark:bg-white/[0.07]`).
  Color get selectionHighlight => isDark
      ? Colors.white.withValues(alpha: 0.07)
      : sidebarForeground.withValues(alpha: 0.055);

  /// Hover/quiet fill (`bg-muted/65`, `hover:bg-muted`).
  Color get quietFill => isDark
      ? Colors.white.withValues(alpha: 0.05)
      : muted.withValues(alpha: 0.65);

  /// User message slab — on the web the user bubble is the primary colour.
  Color get userBubble => primary;
  Color get userBubbleText => primaryForeground;

  /// Assistant message reads as plain text on the canvas.
  Color get assistantBubble => Colors.transparent;
  Color get assistantBubbleText => foreground;

  /// Scrim for lightboxes / image previews.
  Color scrim([double alpha = 0.75]) =>
      const Color(0xFF000000).withValues(alpha: alpha);
}

/// Carries a [WebPalette] through [ThemeData.extensions] so any widget can ask
/// for the active tokens with `WebPalette.of(context)`.
class WebPaletteHolder extends ThemeExtension<WebPaletteHolder> {
  const WebPaletteHolder(this.palette);
  final WebPalette palette;
  @override
  WebPaletteHolder copyWith({WebPalette? palette}) =>
      WebPaletteHolder(palette ?? this.palette);
  @override
  WebPaletteHolder lerp(ThemeExtension<WebPaletteHolder>? other, double t) =>
      this;
}

/// Radius scale from `globals.css` (`--radius-*`).
class WebRadii {
  const WebRadii._();
  static const double base = 7; // --radius: 0.4375rem
  static const double mark = 4; // --radius-mark
  static const double compact = 8; // --radius-compact
  static const double control = 12; // --radius-control
  static const double floating = 18; // --radius-floating
  static const double panel = 22; // --radius-panel
  static const double modal = 22; // --radius-modal
  static const double prominent = 28; // --radius-prominent
  static const double pill = 999;

  static BorderRadius get controlAll => BorderRadius.circular(control);
  static BorderRadius get panelAll => BorderRadius.circular(panel);
  static BorderRadius get modalAll => BorderRadius.circular(modal);
  static BorderRadius get prominentAll => BorderRadius.circular(prominent);
  static BorderRadius get pillAll => BorderRadius.circular(pill);
  static BorderRadius get floatingAll => BorderRadius.circular(floating);
}

/// Type scale actually used by the web shell (px values from the classNames).
class WebType {
  const WebType._();
  static const double headerTitle = 12; // thread header title
  static const double sidebarAction = 12.5; // sidebar action buttons
  static const double settingsNav = 13; // settings nav items
  static const double sectionTitle = 13; // settings group titles
  static const double body = 14; // settings row titles
  static const double rowDescription = 12; // settings row descriptions
  static const double message = 15; // thread messages
  static const double pageTitle = 18; // settings page title
  static const double dialogTitle = 20;
  static const double composerInput = 16;

  /// `--cjk-line-height`: 1.625 for the markdown body, 1.75 for the user turn.
  static const double messageLineHeight = 1.625;
  static const double userMessageLineHeight = 1.75;
}

/// Spacing tokens (Tailwind's 4px grid, as used in the shell).
class WebSpace {
  const WebSpace._();
  static const double xs = 4;
  static const double sm = 8;
  static const double md = 12;
  static const double lg = 16;
  static const double xl = 20;
  static const double xxl = 28;

  /// Minimum touch target the web enforces on mobile (`touch-target` = 44px).
  static const double touchTarget = 44;

  /// Sidebar width on mobile: `min(272px, calc(100vw - 0.75rem))`.
  static const double sidebarWidth = 272;
}
