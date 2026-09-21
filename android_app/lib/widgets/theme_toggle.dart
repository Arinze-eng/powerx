import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../state/theme_controller.dart';
import '../theme/palette.dart';

/// The sun/moon switch from `webui/src/components/thread/ThreadHeader.tsx`.
///
/// Same behaviour as the browser: it shows the scheme you are *not* in (a sun
/// while dark, a moon while light), and one tap flips the resolved scheme —
/// pinning it, exactly like the web writes `light`/`dark` to `localStorage`
/// instead of leaving it on `system`.
class ThemeToggleButton extends StatelessWidget {
  const ThemeToggleButton({super.key, this.size = 34});

  final double size;

  @override
  Widget build(BuildContext context) {
    final theme = context.watch<ThemeController>();
    final p = context.palette;
    final dark = theme.isDark;
    final label = dark ? 'Switch to light' : 'Switch to dark';

    return Tooltip(
      message: label,
      child: Material(
        color: Colors.transparent,
        shape: const CircleBorder(),
        child: InkWell(
          customBorder: const CircleBorder(),
          onTap: () => theme.toggle(),
          hoverColor: p.accent.withValues(alpha: 0.4),
          child: SizedBox(
            width: size,
            height: size,
            child: Icon(
              dark ? Icons.light_mode_outlined : Icons.dark_mode_outlined,
              size: 18,
              color: p.mutedForeground.withValues(alpha: 0.85),
              semanticLabel: label,
            ),
          ),
        ),
      ),
    );
  }
}
