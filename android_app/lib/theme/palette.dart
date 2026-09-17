import 'package:flutter/material.dart';

/// Central design tokens for the PowerX native client.
///
/// The palette follows the modern "agent workspace" language: a near-black
/// neutral canvas, softly-lifted charcoal surfaces, hairline borders and a
/// single restrained blue accent. Colour is used for meaning only (running,
/// done, error) so long transcripts stay calm to read.
///
/// Everything pulls from here, so a re-skin remains a one-file change.
class Palette {
  const Palette._();

  // ---- Backgrounds (deep -> light) -------------------------------------
  /// App canvas / scaffold. Near-black, slightly cool neutral.
  static const bg0 = Color(0xFF0A0A0B);

  /// App bar, drawer — one step above the canvas.
  static const bg1 = Color(0xFF101012);

  /// Cards and assistant bubbles.
  static const bg2 = Color(0xFF17171A);

  /// Composer, inputs, elevated chips.
  static const bg3 = Color(0xFF1F1F23);

  /// Pressed / hover surface.
  static const bg4 = Color(0xFF2A2A30);

  // ---- Hairlines --------------------------------------------------------
  static const border = Color(0xFF2E2E34);
  static const borderSoft = Color(0xFF212126);

  // ---- Brand accent (the single blue) ----------------------------------
  static const accent = Color(0xFF4C8DFF);
  static const accentSoft = Color(0xFF8FB6FF);
  static const accentDeep = Color(0xFF2F6BE0);

  /// Very low-opacity accent used for selected rows and tinted panels.
  static const accentWash = Color(0x1F4C8DFF);

  // ---- User bubble (a light neutral slab, like the reference) ----------
  static const userBubbleTop = Color(0xFF2A2A30);
  static const userBubbleBottom = Color(0xFF232328);
  static const userText = Color(0xFFF5F5F7);

  // ---- Typography -------------------------------------------------------
  static const textPrimary = Color(0xFFF2F2F4);
  static const textSecondary = Color(0xFFA9A9B2);
  static const textTertiary = Color(0xFF70707A);

  // ---- Semantic ---------------------------------------------------------
  static const success = Color(0xFF4ADE80);
  static const warning = Color(0xFFF5B54A);
  static const danger = Color(0xFFF0616D);

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

  /// Soft cool glow used behind hero surfaces on the landing screen.
  static const heroGlow = RadialGradient(
    center: Alignment.topCenter,
    radius: 1.15,
    colors: [Color(0x1F4C8DFF), Color(0x000A0A0B)],
  );

  // ---- Helpers ----------------------------------------------------------

  /// Translucent black used for code blocks and scrims.
  static Color scrim([double alpha = 0.45]) =>
      const Color(0xFF000000).withValues(alpha: alpha);

  /// Surface tint at a given opacity (useful for subtle panels).
  static Color surfaceTint(double alpha) =>
      bg2.withValues(alpha: alpha.clamp(0.0, 1.0));
}