import 'package:flutter/material.dart';

import '../config.dart';
import '../theme/palette.dart';

/// The two brand marks CDNAI actually ships, mirroring the web app:
///
/// * **Shell mark** ([BrandMark]) — `webui/public/brand/nanobot_mark.svg`, the
///   orange robot shown in the web sidebar header. Used on the splash, the
///   navigation drawer header and assistant avatars. The raster is generated
///   from the same SVG by `tool/generate_brand_assets.sh`, so the APK can never
///   drift from the browser.
/// * **Public mark** ([CdnaiMark]) — the monochrome four-point spark tile from
///   `webui/src/components/brand/CdnaiBrand.tsx`, drawn with a painter so it
///   follows `currentColor` exactly like the SVG does on the web. Used on the
///   sign-in surface, matching `SupabaseAuthPage`.
class BrandAssets {
  const BrandAssets._();

  /// 512px raster of the web's `nanobot_mark.svg`.
  static const String shellMark = 'assets/brand/mark.png';

  /// The mark's intrinsic aspect ratio (759 x 718 in the source SVG).
  static const double shellMarkAspect = 759 / 718;
}

/// The orange robot mark, drawn at [size] square with its aspect ratio kept.
///
/// The artwork is transparent, exactly as it is in the web sidebar, so it sits
/// on whatever surface it is placed over. Pass [radius] to clip it into a
/// rounded tile (used by the splash and the launcher-style surfaces).
class BrandMark extends StatelessWidget {
  const BrandMark({super.key, this.size = 88, this.radius, this.imageUrl});

  final double size;
  final double? radius;

  /// Overrides `assets/brand/mark.png` — tests point this at a local file so
  /// they do not need the asset bundle.
  final String? imageUrl;

  @override
  Widget build(BuildContext context) {
    final image = Image.asset(
      imageUrl ?? BrandAssets.shellMark,
      width: size,
      height: size,
      fit: BoxFit.contain,
      filterQuality: FilterQuality.high,
      semanticLabel: PowerXConfig.appName,
      errorBuilder: (context, error, stack) => _FallbackGlyph(size: size),
    );
    if (radius == null) return SizedBox.square(dimension: size, child: image);
    return ClipRRect(
      borderRadius: BorderRadius.circular(radius!),
      child: SizedBox.square(dimension: size, child: image),
    );
  }
}

/// Neutral stand-in if the bundle is missing (e.g. a stripped debug build), so
/// the UI degrades instead of showing a red error box.
class _FallbackGlyph extends StatelessWidget {
  const _FallbackGlyph({required this.size});
  final double size;

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    return Center(
      child: Icon(
        Icons.auto_awesome,
        size: size * 0.6,
        color: p.mutedForeground,
      ),
    );
  }
}

/// The monochrome CDNAI spark tile — a rounded ink square with a four-point
/// spark punched out of it, identical to `CdnaiMark` on the web.
///
/// Because it is drawn rather than loaded, it is crisp at any size and picks up
/// the surrounding ink colour; [tone] mirrors the web component's `tone` prop.
class CdnaiMark extends StatelessWidget {
  const CdnaiMark({
    super.key,
    this.size = 32,
    this.tone = CdnaiTone.auto,
    this.color,
  });

  final double size;
  final CdnaiTone tone;

  /// Explicit tile colour; overrides [tone] when set.
  final Color? color;

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    final tile = color ?? (tone == CdnaiTone.ink ? const Color(0xFF0A0A0A) : p.foreground);
    // `INK.canvas` is pure white on the public surface, the app canvas inside
    // the shell — the punched-out shapes always read as "cut out".
    final punch = tone == CdnaiTone.ink ? const Color(0xFFFFFFFF) : p.background;
    return SizedBox.square(
      dimension: size,
      child: CustomPaint(
        painter: _CdnaiMarkPainter(tile: tile, punch: punch),
        isComplex: false,
      ),
    );
  }
}

enum CdnaiTone { auto, ink }

class _CdnaiMarkPainter extends CustomPainter {
  const _CdnaiMarkPainter({required this.tile, required this.punch});

  final Color tile;
  final Color punch;

  @override
  void paint(Canvas canvas, Size size) {
    // The web SVG is authored on a 40x40 grid; everything below is in those
    // units scaled to `size`.
    final s = size.width / 40;
    double x(double v) => v * s;
    double d(double v) => v * s;

    // Rounded tile: rect(1,1,38,38) rx=11.
    canvas.drawRRect(
      RRect.fromRectAndRadius(
        Rect.fromLTWH(d(1), d(1), d(38), d(38)),
        Radius.circular(d(11)),
      ),
      Paint()..color = tile,
    );

    // Four-point spark, cubic béziers straight from the component's path data.
    final spark = Path()
      ..moveTo(x(20), d(9.5))
      ..cubicTo(x(20.95), d(14.1), x(22.75), d(16.4), x(27.35), d(17.35))
      ..cubicTo(x(22.75), d(18.3), x(20.95), d(20.6), x(20), d(25.2))
      ..cubicTo(x(19.05), d(20.6), x(17.25), d(18.3), x(12.65), d(17.35))
      ..cubicTo(x(17.25), d(16.4), x(19.05), d(14.1), x(20), d(9.5))
      ..close();
    canvas.drawPath(spark, Paint()..color = punch);

    // Companion dot: cx 27.6 cy 27.2 r 2.9 at 92% opacity.
    canvas.drawCircle(
      Offset(x(27.6), d(27.2)),
      d(2.9),
      Paint()..color = punch.withValues(alpha: 0.92),
    );
  }

  @override
  bool shouldRepaint(_CdnaiMarkPainter old) =>
      old.tile != tile || old.punch != punch;
}

/// Circular avatar used for chat turns and the account row.
///
/// The assistant gets the CDNAI mark on a card disc (readable in both themes);
/// humans get a warm neutral disc with their initial, matching the familiar
/// chat-app convention.
class ChatAvatar extends StatelessWidget {
  const ChatAvatar({
    super.key,
    required this.isAssistant,
    this.initial = '',
    this.size = 28,
    this.markUrl,
  });

  final bool isAssistant;
  final String initial;
  final double size;
  final String? markUrl;

  /// Diameter of the mark inside the assistant disc, `h-4 w-4`-ish on a 28px
  /// avatar — the same proportion the web sidebar header uses.
  double get _markSize => size * 0.62;

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    if (isAssistant) {
      return Container(
        width: size,
        height: size,
        decoration: BoxDecoration(
          color: p.card,
          shape: BoxShape.circle,
          border: Border.all(color: p.hairline),
        ),
        alignment: Alignment.center,
        child: BrandMark(size: _markSize, imageUrl: markUrl),
      );
    }
    final letter = initial.trim().isEmpty
        ? '?'
        : initial.trim().substring(0, 1).toUpperCase();
    return Container(
      width: size,
      height: size,
      decoration: BoxDecoration(
        color: p.card,
        shape: BoxShape.circle,
        border: Border.all(color: p.border),
      ),
      alignment: Alignment.center,
      child: Text(
        letter,
        style: TextStyle(
          fontSize: size * 0.44,
          fontWeight: FontWeight.w600,
          color: p.mutedForeground,
        ),
      ),
    );
  }
}

/// Mark + "CDNAI" wordmark, matching `CdnaiLogo`.
///
/// The wordmark is serif on the web (the editorial pairing the brand uses for
/// display type); Android resolves the `serif` family to Noto Serif, which is
/// the closest system equivalent and needs no bundled font.
class BrandWordmark extends StatelessWidget {
  const BrandWordmark({
    super.key,
    this.fontSize = 19,
    this.showMark = true,
    this.tone = CdnaiTone.auto,
    this.color,
  });

  final double fontSize;
  final bool showMark;
  final CdnaiTone tone;

  /// Explicit ink colour for both the mark and the text.
  final Color? color;

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    final ink = color ??
        (tone == CdnaiTone.ink ? const Color(0xFF0A0A0A) : p.foreground);
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        if (showMark) ...[
          CdnaiMark(size: fontSize * 1.4, tone: tone, color: color),
          SizedBox(width: fontSize * 0.55),
        ],
        Text(
          PowerXConfig.appName,
          style: TextStyle(
            fontSize: fontSize,
            fontWeight: FontWeight.w500,
            letterSpacing: -0.2,
            fontFamily: 'serif',
            color: ink,
          ),
        ),
      ],
    );
  }
}
