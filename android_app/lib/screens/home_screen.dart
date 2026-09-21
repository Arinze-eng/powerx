import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../models.dart';
import '../state/app_state.dart';
import '../theme/palette.dart';
import '../utils/session_groups.dart';
import '../widgets/brand.dart';
import '../widgets/sidebar.dart';
import '../widgets/sidebar_actions.dart';
import '../widgets/theme_toggle.dart';
import 'chat_screen.dart';
import 'settings_screen.dart';
import 'settings_sections.dart';

class HomeScreen extends StatefulWidget {
  const HomeScreen({super.key});

  @override
  State<HomeScreen> createState() => _HomeScreenState();
}

class _HomeScreenState extends State<HomeScreen> {
  final GlobalKey<ScaffoldState> _scaffold = GlobalKey<ScaffoldState>();
  final ScrollController _recentScroll = ScrollController();

  /// 0 = Tasks (recent work), 1 = Agent (start something new).
  int _tab = 0;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) {
      final state = context.read<AppState>();
      state.loadSessions();
      state.refreshCredits();
    });
  }

  @override
  void dispose() {
    _recentScroll.dispose();
    super.dispose();
  }

  Future<void> _newChat({String? prompt}) async {
    _scaffold.currentState?.closeDrawer();
    await Navigator.of(context).push(
      MaterialPageRoute(
        builder: (_) => ChatScreen(session: null, initialPrompt: prompt),
      ),
    );
    if (mounted) context.read<AppState>().loadSessions();
  }

  Future<void> _openSession(SessionSummary s) async {
    _scaffold.currentState?.closeDrawer();
    await Navigator.of(
      context,
    ).push(MaterialPageRoute(builder: (_) => ChatScreen(session: s)));
    if (mounted) context.read<AppState>().loadSessions();
  }

  /// True while a delete round-trip is in flight, so the menu cannot be
  /// fired twice for the same conversation.
  bool _deleting = false;

  /// Delete with full feedback: the gateway refuses (HTTP 200 +
  /// `blocked_by_automations`) when scheduled automations are attached, so the
  /// user is told exactly what blocks the delete and offered a force option.
  Future<void> _delete(SessionSummary session) async {
    if (_deleting) return;
    final confirmed = await showDialog<bool>(
      context: context,
      builder:
          (_) => AlertDialog(
            title: const Text('Delete conversation?'),
            content: Text(
              '"${session.displayTitle}" and its history will be '
              'removed. This cannot be undone.',
            ),
            actions: [
              TextButton(
                onPressed: () => Navigator.pop(context, false),
                child: const Text('Cancel'),
              ),
              TextButton(
                onPressed: () => Navigator.pop(context, true),
                child:  Text(
                  'Delete',
                  style: TextStyle(color: Palette.danger),
                ),
              ),
            ],
          ),
    );
    if (confirmed != true || !mounted) return;

    setState(() => _deleting = true);
    try {
      final result = await context.read<AppState>().deleteSession(session);
      if (!mounted) return;
      if (result.deleted) {
        _snack('Conversation deleted.');
        return;
      }
      if (result.blockedByAutomations) {
        final names =
            result.automations.isEmpty
                ? 'a scheduled automation'
                : result.automations.join(', ');
        final force = await showDialog<bool>(
          context: context,
          builder:
              (_) => AlertDialog(
                title: const Text('Automation attached'),
                content: Text(
                  'This chat still has $names attached. Delete the '
                  'conversation and its automations?',
                ),
                actions: [
                  TextButton(
                    onPressed: () => Navigator.pop(context, false),
                    child: const Text('Keep'),
                  ),
                  TextButton(
                    onPressed: () => Navigator.pop(context, true),
                    child:  Text(
                      'Delete both',
                      style: TextStyle(color: Palette.danger),
                    ),
                  ),
                ],
              ),
        );
        if (force == true && mounted) {
          final forced = await context.read<AppState>().deleteSession(
            session,
            deleteAutomations: true,
          );
          if (!mounted) return;
          _snack(
            forced.deleted
                ? 'Conversation and automations deleted.'
                : 'The server did not delete this conversation.',
          );
        }
        return;
      }
      _snack('The server did not delete this conversation.');
    } catch (e) {
      if (mounted) _snack('Delete failed: $e');
    } finally {
      if (mounted) setState(() => _deleting = false);
    }
  }

  void _snack(String msg) {
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(msg)));
  }

  @override
  Widget build(BuildContext context) {
    final state = context.watch<AppState>();
    return Scaffold(
      key: _scaffold,
      backgroundColor: Palette.bg0,
      drawer: SidebarPanel(
        sessions: state.sessions,
        sessionsLoading: state.sessionsLoading,
        activeKey: null,
        newChatActive: true,
        socketStatus: state.socketStatus,
        credits: state.credits,
        footer: _CreditStrip(
          state: state,
          onTap: () {
            _scaffold.currentState?.closeDrawer();
            Navigator.of(context).push(
              MaterialPageRoute(builder: (_) => const SettingsScreen()),
            );
          },
        ),
        onNewChat: () => _newChat(),
        onOpenSession: _openSession,
        onCloseDrawer: () => _scaffold.currentState?.closeDrawer(),
        onRequestDelete: _delete,
        onOpenSection: (action) {
          _scaffold.currentState?.closeDrawer();
          switch (action) {
            case SidebarAction.newChat:
              _newChat();
            case SidebarAction.search:
              break;
            default:
              Navigator.of(context).push(
                MaterialPageRoute(
                  builder:
                      (_) => SettingsScreen(
                        initialSection: SettingsSection.fromId(action.sectionId),
                      ),
                ),
              );
          }
        },
      ),
      floatingActionButton: FloatingActionButton(
        onPressed: () => _newChat(),
        tooltip: 'New task',
        child: const Icon(Icons.add_comment_rounded, size: 23),
      ),
      body: SafeArea(
        child: Column(
          children: [
            _WorkspaceBar(
              tab: _tab,
              onTab: (t) => setState(() => _tab = t),
              onMenu: () => _scaffold.currentState?.openDrawer(),
              onSearch: () => _scaffold.currentState?.openDrawer(),
            ),
            Expanded(
              child: IndexedStack(
                index: _tab,
                children: [
                  _TasksView(
                    state: state,
                    controller: _recentScroll,
                    onOpen: _openSession,
                    onNew: () => _newChat(),
                  ),
                  _AgentView(
                    state: state,
                    onStarter: (p) => _newChat(prompt: p),
                  ),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}

/// Top bar: account avatar, segmented Tasks/Agent switch, search.
class _WorkspaceBar extends StatelessWidget {
  const _WorkspaceBar({
    required this.tab,
    required this.onTab,
    required this.onMenu,
    required this.onSearch,
  });
  final int tab;
  final ValueChanged<int> onTab;
  final VoidCallback onMenu;
  final VoidCallback onSearch;

  @override
  Widget build(BuildContext context) {
    final state = context.watch<AppState>();
    return Padding(
      padding: const EdgeInsets.fromLTRB(8, 6, 8, 8),
      child: Row(
        children: [
          // Account avatar doubles as the history drawer handle.
          InkWell(
            onTap: onMenu,
            borderRadius: BorderRadius.circular(20),
            child: Padding(
              padding: const EdgeInsets.all(4),
              child: ChatAvatar(
                isAssistant: false,
                initial: state.greetingName,
                size: 30,
              ),
            ),
          ),
          const SizedBox(width: 6),
          Expanded(
            child: Center(
              child: Container(
                decoration: BoxDecoration(
                  color: Palette.bg2,
                  borderRadius: BorderRadius.circular(12),
                  border: Border.all(color: Palette.borderSoft),
                ),
                padding: const EdgeInsets.all(3),
                child: Row(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    _SegTab(
                      label: 'Tasks',
                      selected: tab == 0,
                      onTap: () => onTab(0),
                    ),
                    _SegTab(
                      label: 'Agent',
                      selected: tab == 1,
                      onTap: () => onTab(1),
                    ),
                  ],
                ),
              ),
            ),
          ),
          const SizedBox(width: 6),
          const ThemeToggleButton(),
          IconButton(
            onPressed: onSearch,
            icon: const Icon(Icons.search_rounded, size: 21),
            tooltip: 'Search conversations',
          ),
        ],
      ),
    );
  }
}

class _SegTab extends StatelessWidget {
  const _SegTab({
    required this.label,
    required this.selected,
    required this.onTap,
  });
  final String label;
  final bool selected;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: onTap,
      behavior: HitTestBehavior.opaque,
      child: AnimatedContainer(
        duration: const Duration(milliseconds: 150),
        curve: Curves.easeOut,
        padding: const EdgeInsets.symmetric(horizontal: 18, vertical: 7),
        decoration: BoxDecoration(
          color: selected ? Palette.bg4 : Colors.transparent,
          borderRadius: BorderRadius.circular(9),
        ),
        child: Text(
          label,
          style: TextStyle(
            fontSize: 13.5,
            fontWeight: selected ? FontWeight.w700 : FontWeight.w500,
            color: selected ? Palette.textPrimary : Palette.textTertiary,
          ),
        ),
      ),
    );
  }
}

/// Tasks tab: quick-action tiles plus the recent-work feed.
class _TasksView extends StatelessWidget {
  const _TasksView({
    required this.state,
    required this.controller,
    required this.onOpen,
    required this.onNew,
  });
  final AppState state;
  final ScrollController controller;
  final void Function(SessionSummary) onOpen;
  final VoidCallback onNew;

  @override
  Widget build(BuildContext context) {
    final sessions = state.sessions;
    return RefreshIndicator(
      onRefresh: () => state.loadSessions(),
      color: Palette.accent,
      backgroundColor: Palette.bg2,
      child: CustomScrollView(
        controller: controller,
        slivers: [
          SliverPadding(
            padding: const EdgeInsets.fromLTRB(12, 2, 12, 4),
            sliver: SliverToBoxAdapter(
              child: Row(
                children: [
                  Expanded(
                    child: _QuickTile(
                      icon: Icons.workspace_premium_outlined,
                      label: 'Upgrade',
                      onTap: () => Navigator.of(context).pushNamed('/settings'),
                    ),
                  ),
                  const SizedBox(width: 8),
                  Expanded(
                    child: _QuickTile(
                      icon: Icons.view_kanban_outlined,
                      label: 'Projects',
                      onTap: onNew,
                    ),
                  ),
                  const SizedBox(width: 8),
                  Expanded(
                    child: _QuickTile(
                      icon: Icons.menu_book_outlined,
                      label: 'Library',
                      onTap: () => Navigator.of(context).pushNamed('/settings'),
                    ),
                  ),
                ],
              ),
            ),
          ),
          SliverPadding(
            padding: const EdgeInsets.fromLTRB(16, 14, 16, 6),
            sliver: SliverToBoxAdapter(
              child: Row(
                children: [
                   Text(
                    'RECENT',
                    style: TextStyle(
                      color: Palette.textTertiary,
                      fontSize: 11.5,
                      fontWeight: FontWeight.w700,
                      letterSpacing: 1.0,
                    ),
                  ),
                  const Spacer(),
                  if (state.sessionsLoading)
                     SizedBox(
                      width: 13,
                      height: 13,
                      child: CircularProgressIndicator(
                        strokeWidth: 1.8,
                        color: Palette.textTertiary,
                      ),
                    ),
                ],
              ),
            ),
          ),
          if (sessions.isEmpty)
            SliverFillRemaining(
              hasScrollBody: false,
              child: Padding(
                padding: const EdgeInsets.symmetric(horizontal: 34),
                child: Column(
                  mainAxisAlignment: MainAxisAlignment.center,
                  children: [
                    const BrandMark(size: 62),
                    const SizedBox(height: 18),
                    Text(
                      'Hi ${state.greetingName}',
                      style:  TextStyle(
                        fontSize: 20,
                        fontWeight: FontWeight.w800,
                        color: Palette.textPrimary,
                      ),
                    ),
                    const SizedBox(height: 8),
                     Text(
                      'No tasks yet. Start one and it keeps running\n'
                      'in the cloud even if you close the app.',
                      textAlign: TextAlign.center,
                      style: TextStyle(
                        color: Palette.textTertiary,
                        fontSize: 13.5,
                        height: 1.5,
                      ),
                    ),
                  ],
                ),
              ),
            )
          else
            SliverPadding(
              padding: const EdgeInsets.fromLTRB(10, 0, 10, 96),
              sliver: SliverList.builder(
                itemCount: sessions.length,
                itemBuilder:
                    (_, i) => _TaskRow(
                      key: ValueKey(sessions[i].key),
                      session: sessions[i],
                      onTap: () => onOpen(sessions[i]),
                    ),
              ),
            ),
        ],
      ),
    );
  }
}

/// Agent tab: the entry point for a fresh task, with starter prompts.
class _AgentView extends StatelessWidget {
  const _AgentView({required this.state, required this.onStarter});
  final AppState state;
  final void Function(String prompt) onStarter;

  @override
  Widget build(BuildContext context) {
    return ListView(
      padding: const EdgeInsets.fromLTRB(18, 6, 18, 96),
      children: [
        const SizedBox(height: 8),
        const Center(child: BrandMark(size: 76)),
        const SizedBox(height: 22),
        Center(
          child: Text(
            'Hi ${state.greetingName}',
            style:  TextStyle(
              fontSize: 24,
              fontWeight: FontWeight.w800,
              color: Palette.textPrimary,
            ),
          ),
        ),
        const SizedBox(height: 8),
         Center(
          child: Text(
            'Assign a task. It runs in the cloud and keeps going\n'
            'even when this app is closed.',
            textAlign: TextAlign.center,
            style: TextStyle(
              color: Palette.textTertiary,
              fontSize: 14,
              height: 1.5,
            ),
          ),
        ),
        const SizedBox(height: 24),
        FilledButton.icon(
          onPressed: () => onStarter(''),
          icon: const Icon(Icons.bolt_rounded, size: 19),
          label: const Text('Start a new task'),
          style: FilledButton.styleFrom(minimumSize: const Size.fromHeight(50)),
        ),
        const SizedBox(height: 26),
         Text(
          'TRY ASKING FOR',
          style: TextStyle(
            color: Palette.textTertiary,
            fontSize: 11.5,
            fontWeight: FontWeight.w700,
            letterSpacing: 1.0,
          ),
        ),
        const SizedBox(height: 10),
        for (final p in starterPrompts)
          Padding(
            padding: const EdgeInsets.only(bottom: 9),
            child: _StarterCard(
              prompt: p,
              onTap: () => onStarter(p.prompt),
            ),
          ),
      ],
    );
  }
}

class _QuickTile extends StatelessWidget {
  const _QuickTile({
    required this.icon,
    required this.label,
    required this.onTap,
  });
  final IconData icon;
  final String label;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return Material(
      color: Palette.bg2,
      borderRadius: BorderRadius.circular(14),
      child: InkWell(
        borderRadius: BorderRadius.circular(14),
        onTap: onTap,
        child: Container(
          padding: const EdgeInsets.symmetric(vertical: 14),
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(14),
            border: Border.all(color: Palette.borderSoft),
          ),
          child: Column(
            children: [
              Icon(icon, size: 20, color: Palette.accentSoft),
              const SizedBox(height: 7),
              Text(
                label,
                style:  TextStyle(
                  color: Palette.textSecondary,
                  fontSize: 12.5,
                  fontWeight: FontWeight.w600,
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

/// One row in the recent-work feed. Rows are two-line (title + preview) and
/// show relative recency, the way the reference workspace does.
class _TaskRow extends StatelessWidget {
  const _TaskRow({super.key, required this.session, required this.onTap});
  final SessionSummary session;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 2),
      child: Material(
        color: Colors.transparent,
        borderRadius: BorderRadius.circular(12),
        child: InkWell(
          borderRadius: BorderRadius.circular(12),
          onTap: onTap,
          child: Padding(
            padding: const EdgeInsets.fromLTRB(12, 11, 12, 11),
            child: Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                 Padding(
                  padding: EdgeInsets.only(top: 2),
                  child: Icon(
                    Icons.chat_bubble_outline_rounded,
                    size: 16,
                    color: Palette.textTertiary,
                  ),
                ),
                const SizedBox(width: 11),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        session.displayTitle,
                        maxLines: 2,
                        overflow: TextOverflow.ellipsis,
                        style:  TextStyle(
                          fontSize: 14.5,
                          height: 1.3,
                          color: Palette.textPrimary,
                        ),
                      ),
                      if (session.preview.isNotEmpty) ...[
                        const SizedBox(height: 3),
                        Text(
                          session.preview,
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style:  TextStyle(
                            fontSize: 12,
                            color: Palette.textTertiary,
                          ),
                        ),
                      ],
                    ],
                  ),
                ),
                if (session.updatedAt != null) ...[
                  const SizedBox(width: 8),
                  Text(
                    relativeDayLabel(session.updatedAt!),
                    style:  TextStyle(
                      fontSize: 11,
                      color: Palette.textTertiary,
                    ),
                  ),
                ],
              ],
            ),
          ),
        ),
      ),
    );
  }
}

/// A tappable starting point that opens a new chat with the prompt prefilled.
class _StarterCard extends StatelessWidget {
  const _StarterCard({required this.prompt, required this.onTap});
  final StarterPrompt prompt;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return Material(
      color: Palette.bg2,
      borderRadius: BorderRadius.circular(14),
      child: InkWell(
        borderRadius: BorderRadius.circular(14),
        onTap: onTap,
        child: Container(
          padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(14),
            border: Border.all(color: Palette.borderSoft),
          ),
          child: Row(
            children: [
              Container(
                width: 38,
                height: 38,
                decoration: BoxDecoration(
                  color: Palette.accentWash,
                  borderRadius: BorderRadius.circular(11),
                ),
                child: Center(
                  child: Text(
                    prompt.icon,
                    style: const TextStyle(fontSize: 18),
                  ),
                ),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      prompt.title,
                      style:  TextStyle(
                        fontSize: 14.5,
                        fontWeight: FontWeight.w700,
                        color: Palette.textPrimary,
                      ),
                    ),
                    const SizedBox(height: 2),
                    Text(
                      prompt.subtitle,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style:  TextStyle(
                        fontSize: 12.5,
                        color: Palette.textTertiary,
                      ),
                    ),
                  ],
                ),
              ),
               Icon(
                Icons.arrow_outward_rounded,
                size: 16,
                color: Palette.textTertiary,
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _CreditStrip extends StatelessWidget {
  const _CreditStrip({required this.state, required this.onTap});
  final AppState state;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final c = state.credits;
    return InkWell(
      onTap: onTap,
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 18, vertical: 11),
        child: Row(
          children: [
             Icon(Icons.bolt_rounded, size: 18, color: Palette.warning),
            const SizedBox(width: 8),
            Text(
              c != null ? '${c.total} credits' : 'Credits',
              style:  TextStyle(
                color: Palette.textSecondary,
                fontWeight: FontWeight.w600,
                fontSize: 13.5,
              ),
            ),
            const Spacer(),
             Icon(
              Icons.chevron_right_rounded,
              color: Palette.textTertiary,
              size: 20,
            ),
          ],
        ),
      ),
    );
  }
}
