import 'package:flutter/material.dart';

import '../models.dart';
import '../services/supabase_auth.dart';
import '../state/app_state.dart';
import '../theme/palette.dart';
import '../theme/tokens.dart';
import '../utils/session_groups.dart';
import 'brand.dart';
import 'connection_badge.dart';
import 'sidebar_actions.dart';

/// Native navigation drawer built to match `webui/src/components/Sidebar.tsx`.
///
/// On the web the sidebar is an always-visible rail above ~768px and a drawer
/// below it, so a phone user gets: brand mark, the five utility actions
/// (**New chat, Search, Apps, Skills, Automations**) with the sliding selection
/// highlight, the grouped chat list, then **Settings** and the live
/// connection badge pinned at the bottom.
///
/// The utility actions behave exactly as they do in the browser: Apps, Skills
/// and Automations are not separate screens, they open the matching *settings*
/// section (see `Sidebar`'s `onSettingsIntent`).
class SidebarPanel extends StatefulWidget {
  const SidebarPanel({
    super.key,
    required this.sessions,
    required this.sessionsLoading,
    required this.onNewChat,
    required this.onOpenSection,
    required this.onOpenSession,
    required this.onCloseDrawer,
    required this.socketStatus,
    this.activeKey,
    this.email,
    this.archivedCount = 0,
    this.onToggleArchived,
    this.onRequestDelete,
    this.newChatActive = false,
    this.credits,
    this.footer,
  });

  final List<SessionSummary> sessions;
  final bool sessionsLoading;
  final String? activeKey;
  final String? email;
  final int archivedCount;
  final bool newChatActive;
  final AppSocketStatus socketStatus;
  final CreditBundle? credits;

  /// Native extra rendered just above the footer row (the credit balance).
  /// The web has no equivalent, so it never displaces a web element.
  final Widget? footer;

  final VoidCallback onNewChat;
  final ValueChanged<SidebarAction> onOpenSection;
  final ValueChanged<SessionSummary> onOpenSession;
  final VoidCallback onCloseDrawer;
  final VoidCallback? onToggleArchived;
  final ValueChanged<SessionSummary>? onRequestDelete;

  @override
  State<SidebarPanel> createState() => _SidebarPanelState();
}

class _SidebarPanelState extends State<SidebarPanel> {
  final TextEditingController _query = TextEditingController();
  final FocusNode _searchFocus = FocusNode();
  String _filter = '';

  @override
  void dispose() {
    _query.dispose();
    _searchFocus.dispose();
    super.dispose();
  }

  void _focusSearch() {
    if (!_searchFocus.hasFocus) _searchFocus.requestFocus();
  }

  List<SessionSummary> get _visible {
    final q = _filter.trim().toLowerCase();
    if (q.isEmpty) return widget.sessions;
    return widget.sessions.where((s) {
      return s.displayTitle.toLowerCase().contains(q) ||
          s.preview.toLowerCase().contains(q);
    }).toList();
  }

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    final groups = groupSessions(_visible);
    final activeAction = widget.newChatActive || widget.activeKey == null
        ? SidebarAction.newChat
        : null;

    return Drawer(
      backgroundColor: p.sidebar,
      shape: const RoundedRectangleBorder(),
      child: SafeArea(
        child: Column(
          children: [
            // ---- brand row (px-3 pb-2.5 pt-3) ------------------------------
            Padding(
              padding: const EdgeInsets.fromLTRB(12, 12, 12, 10),
              child: Row(
                children: [
                  InkWell(
                    onTap: widget.onCloseDrawer,
                    borderRadius: BorderRadius.circular(WebRadii.control),
                    child: Padding(
                      padding: const EdgeInsets.all(2),
                      child: const BrandMark(size: 32),
                    ),
                  ),
                  const Spacer(),
                  IconButton(
                    onPressed: widget.onCloseDrawer,
                    icon: const Icon(Icons.menu_open_rounded, size: 16),
                    tooltip: 'Close',
                    style: IconButton.styleFrom(
                      minimumSize: const Size(28, 28),
                      padding: EdgeInsets.zero,
                      shape: RoundedRectangleBorder(
                        borderRadius: BorderRadius.circular(WebRadii.compact),
                      ),
                      foregroundColor: p.mutedForeground,
                    ),
                  ),
                ],
              ),
            ),

            // ---- utility actions (space-y-1.5 px-2 pb-2) -------------------
            Padding(
              padding: const EdgeInsets.fromLTRB(8, 0, 8, 8),
              child: SidebarActionList(
                active: activeAction,
                onSelected: (action) {
                  switch (action) {
                    case SidebarAction.newChat:
                      widget.onNewChat();
                    case SidebarAction.search:
                      _focusSearch();
                    default:
                      widget.onOpenSection(action);
                  }
                },
                archivedCount: widget.archivedCount,
                onToggleArchived: widget.onToggleArchived,
              ),
            ),

            // ---- search ---------------------------------------------------
            Padding(
              padding: const EdgeInsets.fromLTRB(12, 0, 12, 6),
              child: TextField(
                controller: _query,
                focusNode: _searchFocus,
                onChanged: (v) => setState(() => _filter = v),
                style: const TextStyle(fontSize: 13.5),
                decoration: InputDecoration(
                  isDense: true,
                  hintText: 'Search chats',
                  filled: true,
                  fillColor: p.background.withValues(alpha: 0.55),
                  prefixIcon: Icon(
                    Icons.search_rounded,
                    size: 17,
                    color: p.mutedForeground,
                  ),
                  suffixIcon: _filter.isEmpty
                      ? null
                      : IconButton(
                          icon: const Icon(Icons.clear_rounded, size: 16),
                          onPressed: () {
                            _query.clear();
                            setState(() => _filter = '');
                          },
                        ),
                  contentPadding: const EdgeInsets.symmetric(
                    horizontal: 10,
                    vertical: 9,
                  ),
                  border: OutlineInputBorder(
                    borderRadius: BorderRadius.circular(WebRadii.control),
                    borderSide: BorderSide.none,
                  ),
                ),
              ),
            ),

            // ---- chat list ------------------------------------------------
            Expanded(
              child:
                  widget.sessionsLoading && widget.sessions.isEmpty
                      ? Center(
                          child: CircularProgressIndicator(
                            strokeWidth: 2.2,
                            color: p.primary,
                          ),
                        )
                      : RefreshIndicator(
                          color: p.primary,
                          backgroundColor: p.card,
                          onRefresh: () async {},
                          child: ListView(
                            padding: const EdgeInsets.fromLTRB(8, 2, 8, 12),
                            children: [
                              for (final g in groups) ...[
                                Padding(
                                  padding: const EdgeInsets.fromLTRB(
                                    10,
                                    12,
                                    10,
                                    4,
                                  ),
                                  child: Text(
                                    g.label,
                                    style: TextStyle(
                                      color: p.mutedForeground,
                                      fontSize: 11.5,
                                      fontWeight: FontWeight.w600,
                                      letterSpacing: 0.4,
                                    ),
                                  ),
                                ),
                                for (final s in g.rows)
                                  SessionTile(
                                    key: ValueKey(s.key),
                                    session: s,
                                    active: s.key == widget.activeKey,
                                    onTap: () => widget.onOpenSession(s),
                                    onDelete: widget.onRequestDelete == null
                                        ? null
                                        : () => widget.onRequestDelete!(s),
                                  ),
                              ],
                              if (_visible.isEmpty)
                                Padding(
                                  padding: const EdgeInsets.all(24),
                                  child: Text(
                                    widget.sessions.isEmpty
                                        ? 'No chats yet.\nStart one to begin.'
                                        : 'No matches for "$_filter"',
                                    textAlign: TextAlign.center,
                                    style: TextStyle(
                                      color: p.mutedForeground,
                                      fontSize: 13,
                                      height: 1.5,
                                    ),
                                  ),
                                ),
                            ],
                          ),
                        ),
            ),

            // ---- footer: Settings + connection badge ----------------------
            if (widget.footer != null) widget.footer!,
            Divider(height: 1, color: p.border.withValues(alpha: 0.45)),
            const SizedBox(height: 6),
            Padding(
              padding: const EdgeInsets.fromLTRB(8, 0, 8, 8),
              child: Row(
                children: [
                  Expanded(
                    child: SidebarFooterButton(
                      label: 'Settings',
                      icon: Icons.settings_outlined,
                      onTap: () => widget.onOpenSection(SidebarAction.settings),
                    ),
                  ),
                  ConnectionBadge(status: widget.socketStatus),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}
