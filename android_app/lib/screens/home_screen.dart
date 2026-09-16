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
  final int _page = 0;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) {
      final state = context.read<AppState>();
      state.loadSessions();
      state.refreshCredits();
    });
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
      drawer: _SessionsDrawer(
        state: state,
        onNew: () => _newChat(),
        onOpen: _openSession,
      ),
      body: IndexedStack(
        index: _page,
        children: [
          _ChatLanding(
            onMenu: () => _scaffold.currentState?.openDrawer(),
            onNew: () => _newChat(),
            onStarter: (p) => _newChat(prompt: p),
            onOpen: _openSession,
          ),
        ],
      ),
    );
  }
}

/// Landing view shown when no conversation is open: a warm greeting, a few
/// starting points (Manus-style) and a compact list of recent threads.
class _ChatLanding extends StatelessWidget {
  const _ChatLanding({
    required this.onMenu,
    required this.onNew,
    required this.onStarter,
    required this.onOpen,
  });
  final VoidCallback onMenu;
  final VoidCallback onNew;
  final void Function(String prompt) onStarter;
  final void Function(SessionSummary) onOpen;

  @override
  Widget build(BuildContext context) {
    final state = context.watch<AppState>();
    final recent = state.sessions.take(4).toList();
    return Container(
      decoration: const BoxDecoration(gradient: Palette.heroGlow),
      child: SafeArea(
        child: Column(
          children: [
            Padding(
              padding: const EdgeInsets.fromLTRB(6, 6, 6, 2),
              child: Row(
                children: [
                  IconButton(
                    onPressed: onMenu,
                    icon: const Icon(Icons.menu_rounded),
                    tooltip: 'Conversations',
                  ),
                  const Spacer(),
                  const BrandWordmark(fontSize: 15),
                  const Spacer(),
                  IconButton(
                    onPressed:
                        () => Navigator.of(context).pushNamed('/settings'),
                    icon: const Icon(Icons.settings_outlined),
                    tooltip: 'Settings',
                  ),
                ],
              ),
            ),
            Expanded(
              child: ListView(
                padding: const EdgeInsets.fromLTRB(20, 6, 20, 28),
                children: [
                  const SizedBox(height: 14),
                  const Center(child: BrandMark(size: 84)),
                  const SizedBox(height: 26),
                  Center(
                    child: Text(
                      'Hi ${state.greetingName}',
                      style: const TextStyle(
                        fontSize: 25,
                        fontWeight: FontWeight.w800,
                        color: Palette.textPrimary,
                      ),
                    ),
                  ),
                  const SizedBox(height: 8),
                  const Center(
                    child: Text(
                      'What would you like me to work on?',
                      textAlign: TextAlign.center,
                      style: TextStyle(
                        color: Palette.textTertiary,
                        fontSize: 14.5,
                      ),
                    ),
                  ),
                  const SizedBox(height: 26),
                  FilledButton.icon(
                    onPressed: onNew,
                    icon: const Icon(Icons.add_comment_outlined, size: 19),
                    label: const Text('Start a new chat'),
                    style: FilledButton.styleFrom(
                      minimumSize: const Size.fromHeight(50),
                    ),
                  ),
                  const SizedBox(height: 28),
                  const _SectionLabel('Try asking for'),
                  const SizedBox(height: 10),
                  for (final p in starterPrompts)
                    Padding(
                      padding: const EdgeInsets.only(bottom: 9),
                      child: _StarterCard(
                        prompt: p,
                        onTap: () => onStarter(p.prompt),
                      ),
                    ),
                  if (recent.isNotEmpty) ...[
                    const SizedBox(height: 24),
                    const _SectionLabel('Recent'),
                    const SizedBox(height: 8),
                    for (final s in recent)
                      _RecentRow(session: s, onTap: onOpen),
                  ],
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _SectionLabel extends StatelessWidget {
  const _SectionLabel(this.text);
  final String text;

  @override
  Widget build(BuildContext context) {
    return Text(
      text.toUpperCase(),
      style: const TextStyle(
        color: Palette.textTertiary,
        fontSize: 11.5,
        fontWeight: FontWeight.w700,
        letterSpacing: 1.0,
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

class _RecentRow extends StatelessWidget {
  const _RecentRow({required this.session, required this.onTap});
  final SessionSummary session;
  final void Function(SessionSummary) onTap;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 6),
      child: Material(
        color: Palette.bg2,
        borderRadius: BorderRadius.circular(12),
        child: InkWell(
          borderRadius: BorderRadius.circular(12),
          onTap: () => onTap(session),
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
            child: Row(
              children: [
                const Icon(
                  Icons.chat_bubble_outline_rounded,
                  size: 17,
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
                          fontSize: 14,
                          color: Palette.textPrimary,
                        ),
                      ),
                      if (session.preview.isNotEmpty) ...[
                        const SizedBox(height: 2),
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
                if (session.updatedAt != null)
                  Text(
                    relativeDayLabel(session.updatedAt!),
                    style: const TextStyle(
                      fontSize: 11,
                      color: Palette.textTertiary,
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
                  label: const Text('New chat'),
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
