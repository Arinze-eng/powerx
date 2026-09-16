import 'package:flutter/material.dart';

/// Central design tokens for the CDNAI (PowerX) native client.
///
/// The palette is a warm "coffee" brown system: deep espresso backgrounds,
/// caramel accents and cream typography. Every screen pulls its colours from
/// here, so the app reads as one product instead of a set of screens. Keeping
/// the values in a single place also means a future re-skin is a one-file
/// change rather than a hunt through literal hex codes.
class Palette {
  const Palette._();

  // ---- Backgrounds (deep -> light) -------------------------------------
  /// App canvas / scaffold.
  static const bg0 = Color(0xFF0E0A08);

  /// Drawer, app bar — one step above the canvas.
  static const bg1 = Color(0xFF171110);

  /// Cards and assistant bubbles.
  static const bg2 = Color(0xFF211816);

  /// Composer, inputs, elevated chips.
  static const bg3 = Color(0xFF2C211D);

  /// Pressed / hover surface.
  static const bg4 = Color(0xFF3A2B24);

  // ---- Hairlines --------------------------------------------------------
  static const border = Color(0xFF3B2C26);
  static const borderSoft = Color(0xFF2A1F1A);

  // ---- Brand accent (caramel) ------------------------------------------
  static const accent = Color(0xFFC98A5B);
  static const accentSoft = Color(0xFFE3B489);
  static const accentDeep = Color(0xFF8A5A34);

  /// Very low-opacity accent used for selected rows and tinted panels.
  static const accentWash = Color(0x1FC98A5B);

  // ---- User bubble (the brown chat surface) ----------------------------
  static const userBubbleTop = Color(0xFF8A5A34);
  static const userBubbleBottom = Color(0xFF68412A);
  static const userText = Color(0xFFFFF5EA);

  // ---- Typography -------------------------------------------------------
  static const textPrimary = Color(0xFFF4EAE2);
  static const textSecondary = Color(0xFFC3B0A2);
  static const textTertiary = Color(0xFF8E7C6E);

  // ---- Semantic ---------------------------------------------------------
  static const success = Color(0xFF8FBF7F);
  static const warning = Color(0xFFE0A458);
  static const danger = Color(0xFFE5735F);

  // ---- Gradients --------------------------------------------------------
  /// Brand mark / primary button.
  static const brandGradient = LinearGradient(
    begin: Alignment.topLeft,
    end: Alignment.bottomRight,
    colors: [accentDeep, accent, accentSoft],
  );

  /// User message bubble.
  static const userBubbleGradient = LinearGradient(
    begin: Alignment.topLeft,
    end: Alignment.bottomRight,
    colors: [userBubbleTop, userBubbleBottom],
  );

  /// Soft warm glow used behind hero surfaces on the landing screen.
  static const heroGlow = RadialGradient(
    center: Alignment.topCenter,
    radius: 1.1,
    colors: [Color(0x33C98A5B), Color(0x000E0A08)],
  );

  // ---- Helpers ----------------------------------------------------------

  /// Translucent black used for code blocks and scrims.
  static Color scrim([double alpha = 0.45]) =>
      const Color(0xFF000000).withValues(alpha: alpha);

  /// Surface tint at a given opacity (useful for subtle panels).
  static Color surfaceTint(double alpha) =>
      bg2.withValues(alpha: alpha.clamp(0.0, 1.0));
}
