import 'package:flutter/material.dart';

import 'tokens.dart';

/// Legacy colour facade over the WebUI design tokens.
///
/// The first native client shipped its own blue-on-black palette under these
/// names. The product's real skin lives in `webui/src/globals.css`, so every
/// name below is now a *resolver* onto [WebPalette] instead of a fixed colour:
/// `Palette.bg0` is the active theme's canvas, `Palette.accent` is its primary
/// action colour, and so on.
///
/// The indirection exists so the ~180 call sites across the screens did not
/// have to be rewritten by hand in one pass. New code should read tokens
/// directly with `context.palette` or [WebPalette.of] — those are the real API
/// and the ones the theme tests assert against.
///
/// [activate] is called by `ThemeController` whenever the user changes the
/// theme; the whole `MaterialApp` subtree rebuilds at that moment, so every
/// reader picks up the new values.
class Palette {
  const Palette._();

  static WebPalette _active = WebPalette.light;

  /// The palette currently in force.
  static WebPalette get active => _active;

  /// Point the facade at a palette. Called from the theme layer.
  static void activate(WebPalette palette) => _active = palette;

  /// Convenience for the theme controller.
  static void activateBrightness(Brightness brightness) =>
      activate(brightness == Brightness.dark ? WebPalette.dark : WebPalette.light);

  // ---- Surfaces ---------------------------------------------------------
  /// App canvas / scaffold.
  static Color get bg0 => _active.background;

  /// App bar and other chrome — the web paints these on the canvas.
  static Color get bg1 => _active.background;

  /// Cards, menus, popovers, assistant surfaces.
  static Color get bg2 => _active.card;

  /// Inset slabs: code blocks, chips, inputs.
  static Color get bg3 => _active.accent;

  /// Hover / pressed fill.
  static Color get bg4 => _active.accent;

  // ---- Hairlines --------------------------------------------------------
  static Color get border => _active.border;
  static Color get borderSoft => _active.hairlineSoft;

  // ---- Emphasis ---------------------------------------------------------
  /// The web's single strong action colour (near-black on light, near-white on
  /// dark). Replaces the old blue.
  static Color get accent => _active.primary;
  static Color get accentDeep => _active.primary;
  static Color get accentSoft => _active.mutedForeground;
  static Color get accentWash => _active.quietFill;

  /// The brand orange (`--inline-token-highlight`), used for the temporary-chat
  /// control and inline token marks.
  static Color get highlight => _active.highlight;

  // ---- Type -------------------------------------------------------------
  static Color get textPrimary => _active.foreground;
  static Color get textSecondary => _active.mutedForeground;
  static Color get textTertiary => _active.mutedForeground;

  // ---- Semantic ---------------------------------------------------------
  static Color get success => _active.success;
  static Color get warning => _active.warning;
  static Color get danger => _active.destructive;

  // ---- Message bubbles --------------------------------------------------
  static Color get userText => _active.userBubbleText;
  static Color get userBubbleTop => _active.userBubble;
  static Color get userBubbleBottom => _active.userBubble;

  /// Kept for callers that still build the user slab as a gradient. The web
  /// bubble is a flat `bg-secondary/70`, so both stops are the same colour.
  static LinearGradient get userBubbleGradient => LinearGradient(
    begin: Alignment.topLeft,
    end: Alignment.bottomRight,
    colors: [_active.userBubble, _active.userBubble],
  );

  // ---- Brand ------------------------------------------------------------
  /// The CDNAI mark's orange, taken straight from the web's
  /// `public/brand/nanobot_mark.svg` so the native tile matches the artwork.
  static const Color brandOrange = Color(0xFFEF8E30);
  static const Color brandOrangeDeep = Color(0xFFE27223);
  static const Color brandOrangeSoft = Color(0xFFF4A949);
  static const Color brandOrangeDark = Color(0xFFB94D0B);

  static const LinearGradient brandGradient = LinearGradient(
    begin: Alignment.topLeft,
    end: Alignment.bottomRight,
    colors: [brandOrangeSoft, brandOrange, brandOrangeDeep],
  );

  /// Soft warm glow behind hero surfaces. Far quieter than the old blue one —
  /// the web uses no gradients at all on the authenticated shell.
  static RadialGradient get heroGlow => RadialGradient(
    center: Alignment.topCenter,
    radius: 1.15,
    colors: [
      _active.highlight.withValues(alpha: 0.10),
      _active.background.withValues(alpha: 0.0),
    ],
  );

  // ---- Helpers ----------------------------------------------------------

  /// Translucent black used for code blocks and scrims.
  static Color scrim([double alpha = 0.45]) =>
      const Color(0xFF000000).withValues(alpha: alpha);

  /// Surface tint at a given opacity (useful for subtle panels).
  static Color surfaceTint(double alpha) =>
      bg2.withValues(alpha: alpha.clamp(0.0, 1.0));
}

/// Read the active design tokens anywhere a [BuildContext] is available:
///
/// ```dart
/// final p = context.palette;
/// Container(color: p.card, border: Border.all(color: p.hairline));
/// ```
extension PaletteContext on BuildContext {
  WebPalette get palette => WebPalette.of(this);
}
