import 'package:flutter/material.dart';

import '../config.dart';
import '../theme/palette.dart';

/// The CDNAI brand mark: a rounded amber-brown tile with the app's bolt glyph.
/// Used on the splash, sign-in and landing surfaces so the identity is stable.
class BrandMark extends StatelessWidget {
  const BrandMark({super.key, this.size = 88, this.radius});

  final double size;
  final double? radius;

  @override
  Widget build(BuildContext context) {
    return Container(
      width: size,
      height: size,
      decoration: BoxDecoration(
        gradient: Palette.brandGradient,
        borderRadius: BorderRadius.circular(radius ?? size * 0.26),
        boxShadow: [
          BoxShadow(
            color: Palette.accent.withValues(alpha: 0.22),
            blurRadius: size * 0.35,
            spreadRadius: -size * 0.06,
          ),
        ],
      ),
      child: Center(
        child: Text(
          '⚡',
          style: TextStyle(fontSize: size * 0.5, color: Colors.white),
        ),
      ),
    );
  }
}

/// Circular avatar used for chat turns and the account row. The assistant gets
/// the brand gradient with a bolt; humans get a warm neutral disc with their
/// initial, matching the familiar chat-app convention.
class ChatAvatar extends StatelessWidget {
  const ChatAvatar({
    super.key,
    required this.isAssistant,
    this.initial = '',
    this.size = 28,
  });

  final bool isAssistant;
  final String initial;
  final double size;

  @override
  Widget build(BuildContext context) {
    if (isAssistant) {
      return Container(
        width: size,
        height: size,
        decoration: const BoxDecoration(
          gradient: Palette.brandGradient,
          shape: BoxShape.circle,
        ),
        child: Center(
          child: Text('⚡', style: TextStyle(fontSize: size * 0.54)),
        ),
      );
    }
    final letter =
        initial.trim().isEmpty
            ? '?'
            : initial.trim().substring(0, 1).toUpperCase();
    return Container(
      width: size,
      height: size,
      decoration: BoxDecoration(
        color: Palette.bg4,
        shape: BoxShape.circle,
        border: Border.all(color: Palette.border),
      ),
      child: Center(
        child: Text(
          letter,
          style: TextStyle(
            fontSize: size * 0.44,
            fontWeight: FontWeight.w700,
            color: Palette.accentSoft,
          ),
        ),
      ),
    );
  }
}

/// Small "CDNAI" wordmark with its status dot, used in the landing header and
/// the new-chat app bar.
class BrandWordmark extends StatelessWidget {
  const BrandWordmark({super.key, this.fontSize = 16});

  final double fontSize;

  @override
  Widget build(BuildContext context) {
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        Container(
          width: fontSize * 0.72,
          height: fontSize * 0.72,
          decoration: const BoxDecoration(
            gradient: Palette.brandGradient,
            shape: BoxShape.circle,
          ),
        ),
        SizedBox(width: fontSize * 0.4),
        Text(
          PowerXConfig.appName,
          style: TextStyle(
            fontSize: fontSize,
            fontWeight: FontWeight.w800,
            letterSpacing: 0.4,
            color: Palette.textPrimary,
          ),
        ),
      ],
    );
  }
}
