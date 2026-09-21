import 'package:flutter/material.dart';

import '../theme/palette.dart';
import '../theme/tokens.dart';

/// Native re-implementation of the WebUI's settings primitives.
///
/// These are the same shapes as `webui/src/components/settings/shared/
/// SettingsControls.tsx` and `ui/segmented-control.tsx`: a `rounded-panel`
/// surface on the settings canvas with hairline dividers, 62px rows, a 14px
/// title over a 12px muted description, and a single 38x22 switch that keeps
/// the web's one non-monochrome control colour (`#2997FF`).
///
/// Anything that renders settings should be built from these, not from
/// `ListTile`, so the two clients cannot drift.

/// `SettingsSectionTitle` — 13px semibold, 85% ink, one line above a group.
class SettingsSectionTitle extends StatelessWidget {
  const SettingsSectionTitle(this.title, {super.key, this.trailing});

  final String title;
  final Widget? trailing;

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    return Padding(
      padding: const EdgeInsets.fromLTRB(4, 0, 4, 8),
      child: Row(
        children: [
          Expanded(
            child: Text(
              title,
              style: TextStyle(
                fontSize: WebType.sectionTitle,
                fontWeight: FontWeight.w600,
                letterSpacing: -0.1,
                color: p.foreground.withValues(alpha: 0.85),
              ),
            ),
          ),
          if (trailing != null) trailing!,
        ],
      ),
    );
  }
}

/// `SettingsGroup` — one rounded panel with hairline-separated rows.
class SettingsGroup extends StatelessWidget {
  const SettingsGroup({super.key, required this.children, this.padding});

  final List<Widget> children;
  final EdgeInsetsGeometry? padding;

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    final rows = <Widget>[];
    for (var i = 0; i < children.length; i++) {
      if (i > 0) {
        rows.add(Divider(
          height: 1,
          thickness: 1,
          color: p.border.withValues(alpha: 0.45),
        ));
      }
      rows.add(children[i]);
    }
    return Container(
      width: double.infinity,
      decoration: BoxDecoration(
        color: p.settingsSurface,
        borderRadius: WebRadii.panelAll,
        border: Border.all(color: p.border.withValues(alpha: 0.45)),
      ),
      clipBehavior: Clip.antiAlias,
      padding: padding,
      child: Column(mainAxisSize: MainAxisSize.min, children: rows),
    );
  }
}

/// `SettingsRow` — min height 62, title over optional description, trailing
/// control on the right.
class SettingsRow extends StatelessWidget {
  const SettingsRow({
    super.key,
    required this.title,
    this.description,
    this.child,
    this.onTap,
    this.leading,
  });

  final String title;
  final String? description;
  final Widget? child;
  final VoidCallback? onTap;
  final Widget? leading;

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    final content = ConstrainedBox(
      constraints: const BoxConstraints(minHeight: 62),
      child: Padding(
        padding: const EdgeInsets.fromLTRB(16, 14, 16, 14),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.center,
          children: [
            if (leading != null) ...[leading!, const SizedBox(width: 12)],
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                mainAxisSize: MainAxisSize.min,
                children: [
                  Text(
                    title,
                    style: TextStyle(
                      fontSize: WebType.body,
                      fontWeight: FontWeight.w500,
                      height: 1.42,
                      color: p.foreground,
                    ),
                  ),
                  if (description != null && description!.isNotEmpty) ...[
                    const SizedBox(height: 2),
                    Text(
                      description!,
                      style: TextStyle(
                        fontSize: WebType.rowDescription,
                        height: 1.6,
                        color: p.mutedForeground,
                      ),
                    ),
                  ],
                ],
              ),
            ),
            if (child != null) ...[const SizedBox(width: 16), child!],
          ],
        ),
      ),
    );
    if (onTap == null) return content;
    return InkWell(onTap: onTap, child: content);
  }
}

/// `ReadOnlyRow` — a value the app only displays.
class ReadOnlyRow extends StatelessWidget {
  const ReadOnlyRow({
    super.key,
    required this.title,
    required this.value,
    this.description,
  });

  final String title;
  final String value;
  final String? description;

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    return SettingsRow(
      title: title,
      description: description,
      child: ConstrainedBox(
        constraints: const BoxConstraints(maxWidth: 170),
        child: Text(
          value,
          textAlign: TextAlign.right,
          maxLines: 1,
          overflow: TextOverflow.ellipsis,
          style: TextStyle(fontSize: 13, color: p.mutedForeground),
        ),
      ),
    );
  }
}

/// `RestartRequiredNotice` — amber, one line plus an optional action.
class RestartRequiredNotice extends StatelessWidget {
  const RestartRequiredNotice({
    super.key,
    required this.message,
    this.actionLabel,
    this.onAction,
    this.busy = false,
  });

  final String message;
  final String? actionLabel;
  final VoidCallback? onAction;
  final bool busy;

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    const amber = Color(0xFFFFB020);
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.fromLTRB(16, 12, 16, 12),
      decoration: BoxDecoration(
        color: amber.withValues(alpha: 0.08),
        borderRadius: WebRadii.controlAll,
        border: Border.all(color: amber.withValues(alpha: 0.20)),
      ),
      child: Row(
        children: [
          Expanded(
            child: Text(
              message,
              style: TextStyle(
                fontSize: 12.5,
                height: 1.5,
                color: p.isDark ? const Color(0xFFFDE68A) : const Color(0xFF92400E),
              ),
            ),
          ),
          if (onAction != null) ...[
            const SizedBox(width: 12),
            OutlinedButton(
              onPressed: busy ? null : onAction,
              style: OutlinedButton.styleFrom(
                minimumSize: const Size(0, 32),
                padding: const EdgeInsets.symmetric(horizontal: 12),
                backgroundColor: p.background.withValues(alpha: 0.8),
                side: BorderSide(color: p.input),
                shape: const StadiumBorder(),
                textStyle: const TextStyle(
                  fontSize: 12,
                  fontWeight: FontWeight.w600,
                ),
              ),
              child: Text(actionLabel ?? 'Restart'),
            ),
          ],
        ],
      ),
    );
  }
}

/// The web `ToggleButton`: a 38x22 pill with an 18px thumb, `#2997FF` when on.
class WebToggle extends StatelessWidget {
  const WebToggle({
    super.key,
    required this.value,
    this.onChanged,
    this.semanticLabel,
  });

  final bool value;
  final ValueChanged<bool>? onChanged;
  final String? semanticLabel;

  static const double _width = 38;
  static const double _height = 22;
  static const double _thumb = 18;

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    final enabled = onChanged != null;
    final track = value
        ? p.toggleOn
        : p.mutedForeground.withValues(alpha: enabled ? 0.30 : 0.18);
    return Semantics(
      label: semanticLabel,
      toggled: value,
      enabled: enabled,
      child: GestureDetector(
        behavior: HitTestBehavior.opaque,
        onTap: enabled ? () => onChanged!(!value) : null,
        child: SizedBox(
          // A 38px control is below the 44px touch target, so the tap area is
          // padded out to match the rest of the app while the pill stays 38px.
          height: WebSpace.touchTarget,
          child: Center(
            child: AnimatedContainer(
              duration: const Duration(milliseconds: 160),
              curve: Curves.easeOut,
              width: _width,
              height: _height,
              padding: const EdgeInsets.all(2),
              decoration: BoxDecoration(
                color: track,
                borderRadius: BorderRadius.circular(_height / 2),
              ),
              child: AnimatedAlign(
                duration: const Duration(milliseconds: 160),
                curve: Curves.easeOut,
                alignment: value ? Alignment.centerRight : Alignment.centerLeft,
                child: Container(
                  width: _thumb,
                  height: _thumb,
                  decoration: const BoxDecoration(
                    color: Colors.white,
                    shape: BoxShape.circle,
                  ),
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
}

/// `SegmentedControl` — a muted pill with a raised selected segment.
class WebSegmentedControl<T> extends StatelessWidget {
  const WebSegmentedControl({
    super.key,
    required this.value,
    required this.options,
    this.onChanged,
  });

  final T value;
  final List<WebSegment<T>> options;
  final ValueChanged<T>? onChanged;

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    return Container(
      padding: const EdgeInsets.all(3),
      decoration: BoxDecoration(
        color: p.muted.withValues(alpha: 0.65),
        borderRadius: BorderRadius.circular(10),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          for (final o in options)
            GestureDetector(
              onTap: onChanged == null ? null : () => onChanged!(o.value),
              child: AnimatedContainer(
                duration: const Duration(milliseconds: 140),
                padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
                decoration: BoxDecoration(
                  color: o.value == value ? p.background : Colors.transparent,
                  borderRadius: BorderRadius.circular(8),
                  border: Border.all(
                    color: o.value == value ? p.hairline : Colors.transparent,
                  ),
                ),
                child: Text(
                  o.label,
                  style: TextStyle(
                    fontSize: 12.5,
                    fontWeight: FontWeight.w500,
                    color: o.value == value ? p.foreground : p.mutedForeground,
                  ),
                ),
              ),
            ),
        ],
      ),
    );
  }
}

class WebSegment<T> {
  const WebSegment(this.value, this.label);
  final T value;
  final String label;
}

/// `CapabilityInstallNotice` / generic inline notice.
class SettingsNotice extends StatelessWidget {
  const SettingsNotice({
    super.key,
    required this.title,
    this.description,
    this.icon = Icons.info_outline_rounded,
  });

  final String title;
  final String? description;
  final IconData icon;

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.fromLTRB(14, 12, 14, 12),
      decoration: BoxDecoration(
        color: p.muted.withValues(alpha: 0.22),
        borderRadius: WebRadii.controlAll,
        border: Border.all(color: p.border.withValues(alpha: 0.55)),
      ),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Padding(
            padding: const EdgeInsets.only(top: 2),
            child: Icon(icon, size: 16, color: p.mutedForeground),
          ),
          const SizedBox(width: 12),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              mainAxisSize: MainAxisSize.min,
              children: [
                Text(
                  title,
                  style: TextStyle(
                    fontSize: 12.5,
                    fontWeight: FontWeight.w500,
                    color: p.foreground,
                  ),
                ),
                if (description != null) ...[
                  const SizedBox(height: 2),
                  Text(
                    description!,
                    style: TextStyle(
                      fontSize: 12,
                      height: 1.66,
                      color: p.mutedForeground,
                    ),
                  ),
                ],
              ],
            ),
          ),
        ],
      ),
    );
  }
}

/// Status pill used for feature/MCP/skill state (`enabled`, `not_installed`…).
class SettingsStatusPill extends StatelessWidget {
  const SettingsStatusPill({super.key, required this.label, required this.tone});

  final String label;
  final SettingsTone tone;

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    final color = switch (tone) {
      SettingsTone.good => p.success,
      SettingsTone.warn => p.warning,
      SettingsTone.bad => p.destructive,
      SettingsTone.quiet => p.mutedForeground,
    };
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 9, vertical: 3),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.12),
        borderRadius: BorderRadius.circular(WebRadii.pill),
      ),
      child: Text(
        label,
        style: TextStyle(fontSize: 11.5, fontWeight: FontWeight.w500, color: color),
      ),
    );
  }
}

enum SettingsTone { good, warn, bad, quiet }
