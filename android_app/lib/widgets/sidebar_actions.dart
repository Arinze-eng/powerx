import 'package:flutter/material.dart';

import '../models.dart';
import '../theme/palette.dart';
import '../theme/tokens.dart';

/// The five utility actions in `webui/src/components/Sidebar.tsx`, plus the
/// archive toggle that only appears once something has been archived.
///
/// The web treats Apps / Skills / Automations as *destinations inside settings*
/// rather than separate screens (`onOpenApps` → `onSettingsIntent`), so the
/// enum carries the matching `SettingsSection` id instead of a route.
enum SidebarAction {
  newChat('New chat', Icons.edit_square),
  search('Search', Icons.search_rounded),
  apps('Apps', Icons.widgets_outlined),
  skills('Skills', Icons.psychology_outlined),
  automations('Automations', Icons.event_repeat_rounded),
  archive('Archive', Icons.inventory_2_outlined),
  settings('Settings', Icons.settings_outlined);

  const SidebarAction(this.label, this.icon);

  final String label;
  final IconData icon;

  /// `?section=` value the matching settings surface opens on.
  String get sectionId => switch (this) {
    SidebarAction.apps => 'apps',
    SidebarAction.skills => 'skills',
    SidebarAction.automations => 'automations',
    _ => 'overview',
  };
}

/// Row metrics, kept next to the widget so the sliding highlight and the
/// buttons cannot disagree about geometry.
class _Metrics {
  const _Metrics._();
  static const double pitch = 40; // tap target
  static const double pillHeight = 32; // the web's `h-8`
  static const double pillInset = 4; // pitch - pill, split top/bottom
  static const double gap = 2; // `space-y-1.5` reads tighter on a phone
}

/// The utility-action stack with the web's sliding selection highlight.
///
/// `SidebarSelectionHighlight` animates a single absolutely-positioned slab
/// between the active rows over 300ms ease-out; this is the Flutter equivalent,
/// so tapping "New chat" on a phone produces the same movement a browser shows.
class SidebarActionList extends StatelessWidget {
  const SidebarActionList({
    super.key,
    required this.active,
    required this.onSelected,
    this.archivedCount = 0,
    this.onToggleArchived,
    this.showArchived = false,
  });

  final SidebarAction? active;
  final ValueChanged<SidebarAction> onSelected;
  final int archivedCount;
  final VoidCallback? onToggleArchived;
  final bool showArchived;

  List<SidebarAction> get _actions => [
    SidebarAction.newChat,
    SidebarAction.search,
    SidebarAction.apps,
    SidebarAction.skills,
    SidebarAction.automations,
    if (archivedCount > 0) SidebarAction.archive,
  ];

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    final actions = _actions;
    final index = actions.indexOf(active ?? SidebarAction.newChat);
    final activeIndex = index < 0 ? null : index;
    final rowPitch = _Metrics.pitch + _Metrics.gap;

    return LayoutBuilder(
      builder: (context, constraints) {
        return Stack(
          children: [
            // ---- sliding highlight ------------------------------------
            AnimatedPositioned(
              duration: const Duration(milliseconds: 300),
              curve: Curves.easeOut,
              left: 0,
              right: 0,
              height: _Metrics.pillHeight,
              top: activeIndex == null
                  ? 0
                  : activeIndex * rowPitch + _Metrics.pillInset,
              child: IgnorePointer(
                child: AnimatedOpacity(
                  duration: const Duration(milliseconds: 150),
                  opacity: activeIndex == null ? 0 : 1,
                  child: DecoratedBox(
                    decoration: BoxDecoration(
                      color: p.selectionHighlight,
                      borderRadius: BorderRadius.circular(WebRadii.control),
                    ),
                  ),
                ),
              ),
            ),

            // ---- buttons ---------------------------------------------
            Column(
              children: [
                for (final a in actions) ...[
                  _SidebarActionButton(
                    action: a,
                    active: a == (active ?? SidebarAction.newChat),
                    // Archive is a state flip, not a destination, so it gets
                    // its own callback on both clients.
                    onTap: a == SidebarAction.archive && onToggleArchived != null
                        ? onToggleArchived!
                        : () => onSelected(a),
                    archived: showArchived,
                  ),
                  if (a != actions.last)
                    const SizedBox(height: _Metrics.gap),
                ],
              ],
            ),
          ],
        );
      },
    );
  }
}

class _SidebarActionButton extends StatelessWidget {
  const _SidebarActionButton({
    required this.action,
    required this.active,
    required this.onTap,
    this.archived = false,
  });

  final SidebarAction action;
  final bool active;
  final VoidCallback onTap;
  final bool archived;

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    final label = action == SidebarAction.archive
        ? (archived ? 'Hide archived' : 'Show archived')
        : action.label;
    final color = active ? p.sidebarAccentForeground : p.sidebarForeground.withValues(alpha: 0.85);

    return SizedBox(
      height: _Metrics.pitch,
      child: Material(
        color: Colors.transparent,
        borderRadius: BorderRadius.circular(WebRadii.control),
        child: InkWell(
          borderRadius: BorderRadius.circular(WebRadii.control),
          onTap: onTap,
          hoverColor: p.sidebarForeground.withValues(alpha: 0.035),
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 12),
            child: Row(
              children: [
                Icon(action.icon, size: 16, color: color),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(
                    label,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: TextStyle(
                      fontSize: WebType.sidebarAction,
                      fontWeight: FontWeight.w500,
                      color: color,
                    ),
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

/// One conversation row.
///
/// Matches the ChatList row: `rounded-control`, 13px medium title, 82% opacity
/// when idle, `bg-sidebar-selected` when it is the open thread.
class SessionTile extends StatelessWidget {
  const SessionTile({
    super.key,
    required this.session,
    required this.onTap,
    this.active = false,
    this.onDelete,
    this.running = false,
  });

  final SessionSummary session;
  final bool active;
  final VoidCallback onTap;
  final VoidCallback? onDelete;
  final bool running;

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    final titleColor = active
        ? p.sidebarAccentForeground
        : p.sidebarForeground.withValues(alpha: 0.82);

    return Padding(
      padding: const EdgeInsets.only(bottom: 2),
      child: Material(
        color: active ? p.sidebarSelected : Colors.transparent,
        borderRadius: BorderRadius.circular(WebRadii.control),
        child: InkWell(
          borderRadius: BorderRadius.circular(WebRadii.control),
          onTap: onTap,
          hoverColor: p.sidebarForeground.withValues(alpha: 0.035),
          child: Padding(
            padding: const EdgeInsets.fromLTRB(8, 6, 2, 6),
            child: Row(
              children: [
                if (running) ...[
                  SizedBox(
                    width: 12,
                    height: 12,
                    child: CircularProgressIndicator(
                      strokeWidth: 1.6,
                      color: p.highlight,
                    ),
                  ),
                  const SizedBox(width: 6),
                ],
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        session.displayTitle,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: TextStyle(
                          fontSize: 13,
                          fontWeight: FontWeight.w500,
                          height: 1.25,
                          color: titleColor,
                        ),
                      ),
                      if (session.preview.isNotEmpty) ...[
                        const SizedBox(height: 2),
                        Text(
                          session.preview,
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: TextStyle(
                            fontSize: 11.5,
                            color: p.mutedForeground.withValues(alpha: 0.8),
                          ),
                        ),
                      ],
                    ],
                  ),
                ),
                if (onDelete != null)
                  _RowMenu(onDelete: onDelete!, destructive: p.destructive),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class _RowMenu extends StatelessWidget {
  const _RowMenu({required this.onDelete, required this.destructive});

  final VoidCallback onDelete;
  final Color destructive;

  @override
  Widget build(BuildContext context) {
    return PopupMenuButton<String>(
      icon: Icon(
        Icons.more_horiz_rounded,
        size: 18,
        color: context.palette.mutedForeground.withValues(alpha: 0.7),
      ),
      tooltip: 'Options',
      padding: EdgeInsets.zero,
      iconSize: 18,
      splashRadius: 16,
      onSelected: (v) {
        if (v == 'delete') onDelete();
      },
      itemBuilder: (_) => [
        PopupMenuItem(
          value: 'delete',
          child: Row(
            children: [
              Icon(Icons.delete_outline_rounded, size: 17, color: destructive),
              const SizedBox(width: 8),
              const Text('Delete'),
            ],
          ),
        ),
      ],
    );
  }
}

/// The full-width action button the sidebar footer uses for Settings — the web
/// keeps it the same shape as the utility actions, just pinned to the bottom.
class SidebarFooterButton extends StatelessWidget {
  const SidebarFooterButton({
    super.key,
    required this.label,
    required this.icon,
    required this.onTap,
  });

  final String label;
  final IconData icon;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    return SizedBox(
      height: _Metrics.pitch,
      child: Material(
        color: Colors.transparent,
        borderRadius: BorderRadius.circular(WebRadii.control),
        child: InkWell(
          borderRadius: BorderRadius.circular(WebRadii.control),
          onTap: onTap,
          hoverColor: p.sidebarForeground.withValues(alpha: 0.035),
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 12),
            child: Row(
              children: [
                Icon(
                  icon,
                  size: 16,
                  color: p.sidebarForeground.withValues(alpha: 0.85),
                ),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(
                    label,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: TextStyle(
                      fontSize: WebType.sidebarAction,
                      fontWeight: FontWeight.w500,
                      color: p.sidebarForeground.withValues(alpha: 0.85),
                    ),
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}
