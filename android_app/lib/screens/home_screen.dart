import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../models.dart';
import '../state/app_state.dart';
import '../theme/palette.dart';
import '../utils/session_groups.dart';
import '../widgets/brand.dart';
import 'chat_screen.dart';

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

  @override
  Widget build(BuildContext context) {
    final state = context.watch<AppState>();
    return Scaffold(
      key: _scaffold,
      backgroundColor: Palette.bg0,
      drawer: _SessionsDrawer(
        state: state,
        onNew: () => _newChat(),
        onOpen: _openSession,
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
                  const Text(
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
                    const SizedBox(
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
                      style: const TextStyle(
                        fontSize: 20,
                        fontWeight: FontWeight.w800,
                        color: Palette.textPrimary,
                      ),
                    ),
                    const SizedBox(height: 8),
                    const Text(
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
            style: const TextStyle(
              fontSize: 24,
              fontWeight: FontWeight.w800,
              color: Palette.textPrimary,
            ),
          ),
        ),
        const SizedBox(height: 8),
        const Center(
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
        const Text(
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
                style: const TextStyle(
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
                const Padding(
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
                        style: const TextStyle(
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
                          style: const TextStyle(
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
                    style: const TextStyle(
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
                      style: const TextStyle(
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
                      style: const TextStyle(
                        fontSize: 12.5,
                        color: Palette.textTertiary,
                      ),
                    ),
                  ],
                ),
              ),
              const Icon(
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

/// History drawer: search, new chat, conversations grouped by recency, credit
/// balance and the account row.
class _SessionsDrawer extends StatefulWidget {
  const _SessionsDrawer({
    required this.state,
    required this.onNew,
    required this.onOpen,
  });
  final AppState state;
  final VoidCallback onNew;
  final void Function(SessionSummary) onOpen;

  @override
  State<_SessionsDrawer> createState() => _SessionsDrawerState();
}

class _SessionsDrawerState extends State<_SessionsDrawer> {
  final TextEditingController _query = TextEditingController();
  String _filter = '';
  bool _deleting = false;

  @override
  void dispose() {
    _query.dispose();
    super.dispose();
  }

  List<SessionSummary> get _visible =>
      filterSessions(widget.state.sessions, _filter);

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
                child: const Text(
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
      final result = await widget.state.deleteSession(session);
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
                    child: const Text(
                      'Delete both',
                      style: TextStyle(color: Palette.danger),
                    ),
                  ),
                ],
              ),
        );
        if (force == true && mounted) {
          final forced = await widget.state.deleteSession(
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
    final state = widget.state;
    final rows = _visible;
    final groups = groupSessions(rows);

    return Drawer(
      backgroundColor: Palette.bg1,
      child: SafeArea(
        child: Column(
          children: [
            Padding(
              padding: const EdgeInsets.fromLTRB(16, 14, 8, 4),
              child: Row(
                children: [
                  const BrandWordmark(fontSize: 15),
                  const Spacer(),
                  IconButton(
                    onPressed: () => Navigator.of(context).pop(),
                    icon: const Icon(Icons.close_rounded),
                    tooltip: 'Close',
                  ),
                ],
              ),
            ),
            Padding(
              padding: const EdgeInsets.fromLTRB(14, 6, 14, 8),
              child: SizedBox(
                width: double.infinity,
                child: FilledButton.icon(
                  onPressed: widget.onNew,
                  icon: const Icon(Icons.add_rounded, size: 19),
                  label: const Text('New task'),
                  style: FilledButton.styleFrom(
                    minimumSize: const Size.fromHeight(46),
                  ),
                ),
              ),
            ),
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 14),
              child: TextField(
                controller: _query,
                onChanged: (v) => setState(() => _filter = v),
                style: const TextStyle(fontSize: 14),
                decoration: InputDecoration(
                  isDense: true,
                  hintText: 'Search conversations',
                  prefixIcon: const Icon(
                    Icons.search_rounded,
                    size: 18,
                    color: Palette.textTertiary,
                  ),
                  suffixIcon:
                      _filter.isEmpty
                          ? null
                          : IconButton(
                            icon: const Icon(Icons.clear_rounded, size: 18),
                            onPressed: () {
                              _query.clear();
                              setState(() => _filter = '');
                            },
                          ),
                  contentPadding: const EdgeInsets.symmetric(
                    horizontal: 12,
                    vertical: 11,
                  ),
                  border: OutlineInputBorder(
                    borderRadius: BorderRadius.circular(12),
                    borderSide: BorderSide.none,
                  ),
                ),
              ),
            ),
            const SizedBox(height: 6),
            Expanded(
              child:
                  state.sessionsLoading && state.sessions.isEmpty
                      ? const Center(
                        child: CircularProgressIndicator(
                          strokeWidth: 2.4,
                          color: Palette.accent,
                        ),
                      )
                      : RefreshIndicator(
                        onRefresh: () => state.loadSessions(),
                        color: Palette.accent,
                        backgroundColor: Palette.bg2,
                        child: ListView(
                          padding: const EdgeInsets.fromLTRB(8, 4, 8, 12),
                          children: [
                            for (final g in groups) ...[
                              Padding(
                                padding: const EdgeInsets.fromLTRB(
                                  10,
                                  14,
                                  10,
                                  6,
                                ),
                                child: Text(
                                  g.label,
                                  style: const TextStyle(
                                    color: Palette.textTertiary,
                                    fontSize: 11.5,
                                    fontWeight: FontWeight.w700,
                                    letterSpacing: 0.7,
                                  ),
                                ),
                              ),
                              for (final s in g.rows)
                                _SessionTile(
                                  key: ValueKey(s.key),
                                  session: s,
                                  onTap: () => widget.onOpen(s),
                                  onDelete: () => _delete(s),
                                ),
                            ],
                            if (rows.isEmpty)
                              Padding(
                                padding: const EdgeInsets.all(26),
                                child: Center(
                                  child: Text(
                                    state.sessions.isEmpty
                                        ? 'No conversations yet.\nStart a new chat to begin.'
                                        : 'No matches for "$_filter"',
                                    textAlign: TextAlign.center,
                                    style: const TextStyle(
                                      color: Palette.textTertiary,
                                      fontSize: 13.5,
                                      height: 1.5,
                                    ),
                                  ),
                                ),
                              ),
                          ],
                        ),
                      ),
            ),
            const Divider(height: 1),
            _CreditStrip(state: state),
            _AccountRow(
              state: state,
              onSettings: () {
                Navigator.of(context).pop();
                Navigator.of(context).pushNamed('/settings');
              },
            ),
          ],
        ),
      ),
    );
  }
}

/// One conversation row: title, preview, recency and a delete action.
class _SessionTile extends StatelessWidget {
  const _SessionTile({
    super.key,
    required this.session,
    required this.onTap,
    required this.onDelete,
  });
  final SessionSummary session;
  final VoidCallback onTap;
  final VoidCallback onDelete;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 2),
      child: Material(
        color: Colors.transparent,
        borderRadius: BorderRadius.circular(11),
        child: InkWell(
          borderRadius: BorderRadius.circular(11),
          onTap: onTap,
          child: Padding(
            padding: const EdgeInsets.fromLTRB(10, 9, 4, 9),
            child: Row(
              children: [
                const Icon(
                  Icons.chat_bubble_outline_rounded,
                  size: 16,
                  color: Palette.textTertiary,
                ),
                const SizedBox(width: 11),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        session.displayTitle,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(
                          color: Palette.textPrimary,
                          fontSize: 14,
                        ),
                      ),
                      if (session.preview.isNotEmpty) ...[
                        const SizedBox(height: 2),
                        Text(
                          session.preview,
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: const TextStyle(
                            color: Palette.textTertiary,
                            fontSize: 12,
                          ),
                        ),
                      ],
                    ],
                  ),
                ),
                PopupMenuButton<String>(
                  icon: const Icon(
                    Icons.more_vert_rounded,
                    color: Palette.textTertiary,
                    size: 18,
                  ),
                  tooltip: 'Options',
                  onSelected: (v) {
                    if (v == 'delete') onDelete();
                  },
                  itemBuilder:
                      (_) => const [
                        PopupMenuItem(
                          value: 'delete',
                          child: Row(
                            children: [
                              Icon(
                                Icons.delete_outline_rounded,
                                size: 17,
                                color: Palette.danger,
                              ),
                              SizedBox(width: 8),
                              Text('Delete'),
                            ],
                          ),
                        ),
                      ],
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class _CreditStrip extends StatelessWidget {
  const _CreditStrip({required this.state});
  final AppState state;

  @override
  Widget build(BuildContext context) {
    final c = state.credits;
    return InkWell(
      onTap: () {
        Navigator.of(context).pop();
        Navigator.of(context).pushNamed('/settings');
      },
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 18, vertical: 11),
        child: Row(
          children: [
            const Icon(Icons.bolt_rounded, size: 18, color: Palette.warning),
            const SizedBox(width: 8),
            Text(
              c != null ? '${c.total} credits' : 'Credits',
              style: const TextStyle(
                color: Palette.textSecondary,
                fontWeight: FontWeight.w600,
                fontSize: 13.5,
              ),
            ),
            const Spacer(),
            const Icon(
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

class _AccountRow extends StatelessWidget {
  const _AccountRow({required this.state, required this.onSettings});
  final AppState state;
  final VoidCallback onSettings;

  @override
  Widget build(BuildContext context) {
    final name =
        state.displayName?.isNotEmpty == true
            ? state.displayName!
            : (state.email ?? 'Signed in');
    return Padding(
      padding: const EdgeInsets.fromLTRB(12, 4, 12, 10),
      child: Row(
        children: [
          ChatAvatar(isAssistant: false, initial: state.greetingName, size: 34),
          const SizedBox(width: 10),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  name,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: const TextStyle(
                    color: Palette.textPrimary,
                    fontSize: 13.5,
                    fontWeight: FontWeight.w600,
                  ),
                ),
                if (state.displayName?.isNotEmpty == true &&
                    (state.email ?? '').isNotEmpty)
                  Text(
                    state.email!,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: const TextStyle(
                      color: Palette.textTertiary,
                      fontSize: 11.5,
                    ),
                  ),
              ],
            ),
          ),
          IconButton(
            icon: const Icon(Icons.settings_outlined, size: 20),
            tooltip: 'Settings',
            onPressed: onSettings,
          ),
          IconButton(
            icon: const Icon(Icons.logout_rounded, size: 20),
            tooltip: 'Sign out',
            onPressed: () => state.signOut(),
          ),
        ],
      ),
    );
  }
}