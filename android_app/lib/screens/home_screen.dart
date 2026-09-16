import 'dart:async';

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../config.dart';
import '../models.dart';
import '../state/app_state.dart';
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

  Future<void> _newChat() async {
    _scaffold.currentState?.closeDrawer();
    await Navigator.of(context).push(MaterialPageRoute(
        builder: (_) => ChatScreen(session: null)));
    if (mounted) context.read<AppState>().loadSessions();
  }

  Future<void> _openSession(SessionSummary s) async {
    _scaffold.currentState?.closeDrawer();
    await Navigator.of(context)
        .push(MaterialPageRoute(builder: (_) => ChatScreen(session: s)));
    if (mounted) context.read<AppState>().loadSessions();
  }

  @override
  Widget build(BuildContext context) {
    final state = context.watch<AppState>();
    return Scaffold(
      key: _scaffold,
      drawer: _SessionsDrawer(state: state, onNew: _newChat, onOpen: _openSession),
      body: IndexedStack(
        index: _page,
        children: [
          _ChatLanding(onMenu: () => _scaffold.currentState?.openDrawer(),
              onNew: _newChat, onOpen: _openSession),
        ],
      ),
    );
  }
}

/// Landing view shown when no conversation is open.
class _ChatLanding extends StatelessWidget {
  const _ChatLanding({
    required this.onMenu,
    required this.onNew,
    required this.onOpen,
  });
  final VoidCallback onMenu;
  final VoidCallback onNew;
  final void Function(SessionSummary) onOpen;

  @override
  Widget build(BuildContext context) {
    final state = context.watch<AppState>();
    final recent = state.sessions.take(4).toList();
    return SafeArea(
      child: Column(
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 8, 8, 8),
            child: Row(
              children: [
                IconButton(
                    onPressed: onMenu, icon: const Icon(Icons.menu_rounded)),
                const Spacer(),
                Text('⚡ ${PowerXConfig.appName}',
                    style: const TextStyle(
                        fontWeight: FontWeight.w800, fontSize: 18)),
                const Spacer(),
                IconButton(
                    onPressed: () =>
                        Navigator.of(context).pushNamed('/settings'),
                    icon: const Icon(Icons.settings_outlined)),
              ],
            ),
          ),
          Expanded(
            child: ListView(
              padding: const EdgeInsets.fromLTRB(24, 8, 24, 24),
              children: [
                const SizedBox(height: 12),
                Center(
                  child: Container(
                    width: 92,
                    height: 92,
                    decoration: BoxDecoration(
                      gradient: const LinearGradient(
                        colors: [Color(0xFF2E7D32), Color(0xFF66BB6A)],
                      ),
                      borderRadius: BorderRadius.circular(24),
                    ),
                    child: const Center(
                        child: Text('⚡', style: TextStyle(fontSize: 46))),
                  ),
                ),
                const SizedBox(height: 20),
                Center(
                  child: Text('Hi ${state.greetingName} 👋',
                      style: const TextStyle(
                          fontSize: 22, fontWeight: FontWeight.w700)),
                ),
                const SizedBox(height: 8),
                const Center(
                  child: Text(
                    'How can I help you today?',
                    textAlign: TextAlign.center,
                    style: TextStyle(color: Colors.white54, fontSize: 15),
                  ),
                ),
                const SizedBox(height: 28),
                Center(
                  child: FilledButton.icon(
                    onPressed: onNew,
                    icon: const Icon(Icons.add_comment_outlined),
                    label: const Text('Start a new chat'),
                    style: FilledButton.styleFrom(
                      backgroundColor: const Color(0xFF2E7D32),
                      minimumSize: const Size(220, 50),
                    ),
                  ),
                ),
                if (recent.isNotEmpty) ...[
                  const SizedBox(height: 32),
                  const Text('Recent',
                      style: TextStyle(
                          color: Colors.white70,
                          fontWeight: FontWeight.w700,
                          fontSize: 13.5)),
                  const SizedBox(height: 6),
                  for (final s in recent)
                    Card(
                      color: const Color(0xFF141B33),
                      margin: const EdgeInsets.symmetric(vertical: 5),
                      child: ListTile(
                        dense: true,
                        leading: const Icon(Icons.history_rounded,
                            color: Colors.white38, size: 20),
                        title: Text(s.displayTitle,
                            maxLines: 1, overflow: TextOverflow.ellipsis),
                        subtitle: s.preview.isEmpty
                            ? null
                            : Text(s.preview,
                                maxLines: 1,
                                overflow: TextOverflow.ellipsis,
                                style: const TextStyle(
                                    color: Colors.white38, fontSize: 12)),
                        onTap: () => onOpen(s),
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

class _SessionsDrawer extends StatefulWidget {
  const _SessionsDrawer(
      {required this.state, required this.onNew, required this.onOpen});
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

  List<SessionSummary> get _visible {
    final q = _filter.trim().toLowerCase();
    if (q.isEmpty) return widget.state.sessions;
    return widget.state.sessions
        .where((s) =>
            s.displayTitle.toLowerCase().contains(q) ||
            s.preview.toLowerCase().contains(q))
        .toList();
  }

  /// Delete with full feedback: the gateway refuses (HTTP 200 +
  /// `blocked_by_automations`) when scheduled automations are attached, so the
  /// user is told exactly what blocks the delete and offered a force option.
  Future<void> _delete(SessionSummary session) async {
    if (_deleting) return;
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (_) => AlertDialog(
        backgroundColor: const Color(0xFF141B33),
        title: const Text('Delete conversation?'),
        content: Text('"${session.displayTitle}" and its history will be '
            'removed. This cannot be undone.'),
        actions: [
          TextButton(
              onPressed: () => Navigator.pop(context, false),
              child: const Text('Cancel')),
          TextButton(
              onPressed: () => Navigator.pop(context, true),
              child:
                  const Text('Delete', style: TextStyle(color: Colors.redAccent))),
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
        final names = result.automations.isEmpty
            ? 'a scheduled automation'
            : result.automations.join(', ');
        final force = await showDialog<bool>(
          context: context,
          builder: (_) => AlertDialog(
            backgroundColor: const Color(0xFF141B33),
            title: const Text('Automation attached'),
            content: Text('This chat still has $names attached. Delete the '
                'conversation and its automations?'),
            actions: [
              TextButton(
                  onPressed: () => Navigator.pop(context, false),
                  child: const Text('Keep')),
              TextButton(
                  onPressed: () => Navigator.pop(context, true),
                  child: const Text('Delete both',
                      style: TextStyle(color: Colors.redAccent))),
            ],
          ),
        );
        if (force == true && mounted) {
          final forced =
              await widget.state.deleteSession(session, deleteAutomations: true);
          if (!mounted) return;
          _snack(forced.deleted
              ? 'Conversation and automations deleted.'
              : 'The server did not delete this conversation.');
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
    ScaffoldMessenger.of(context)
        .showSnackBar(SnackBar(content: Text(msg)));
  }

  @override
  Widget build(BuildContext context) {
    final state = widget.state;
    final rows = _visible;
    return Drawer(
      backgroundColor: const Color(0xFF0E1428),
      child: SafeArea(
        child: Column(
          children: [
            Padding(
              padding: const EdgeInsets.fromLTRB(16, 16, 8, 8),
              child: Row(
                children: [
                  const Text('Conversations',
                      style: TextStyle(
                          fontSize: 18, fontWeight: FontWeight.w800)),
                  const Spacer(),
                  IconButton(
                      onPressed: () => Navigator.of(context).pop(),
                      icon: const Icon(Icons.close_rounded)),
                ],
              ),
            ),
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 16),
              child: TextField(
                controller: _query,
                onChanged: (v) => setState(() => _filter = v),
                style: const TextStyle(fontSize: 14),
                decoration: InputDecoration(
                  isDense: true,
                  hintText: 'Search conversations',
                  hintStyle: const TextStyle(color: Colors.white38),
                  prefixIcon:
                      const Icon(Icons.search_rounded, size: 18, color: Colors.white38),
                  suffixIcon: _filter.isEmpty
                      ? null
                      : IconButton(
                          icon: const Icon(Icons.clear_rounded, size: 18),
                          onPressed: () {
                            _query.clear();
                            setState(() => _filter = '');
                          },
                        ),
                  filled: true,
                  fillColor: const Color(0xFF1A2138),
                  contentPadding:
                      const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
                  border: OutlineInputBorder(
                      borderRadius: BorderRadius.circular(14),
                      borderSide: BorderSide.none),
                ),
              ),
            ),
            const SizedBox(height: 10),
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 16),
              child: SizedBox(
                width: double.infinity,
                child: FilledButton.icon(
                  onPressed: widget.onNew,
                  icon: const Icon(Icons.add_rounded),
                  label: const Text('New chat'),
                  style: FilledButton.styleFrom(
                    backgroundColor: const Color(0xFF2E7D32),
                    minimumSize: const Size.fromHeight(46),
                  ),
                ),
              ),
            ),
            const SizedBox(height: 8),
            Expanded(
              child: state.sessionsLoading && state.sessions.isEmpty
                  ? const Center(child: CircularProgressIndicator())
                  : RefreshIndicator(
                      onRefresh: () => state.loadSessions(),
                      child: ListView(
                        padding: const EdgeInsets.symmetric(vertical: 8),
                        children: [
                          for (final s in rows)
                            ListTile(
                              leading: const Icon(Icons.forum_outlined,
                                  color: Colors.white54),
                              title: Text(s.displayTitle,
                                  maxLines: 1, overflow: TextOverflow.ellipsis),
                              subtitle: s.preview.isEmpty
                                  ? null
                                  : Text(s.preview,
                                      maxLines: 1,
                                      overflow: TextOverflow.ellipsis,
                                      style: const TextStyle(
                                          color: Colors.white38)),
                              onTap: () => widget.onOpen(s),
                              trailing: PopupMenuButton<String>(
                                icon: const Icon(Icons.more_vert_rounded,
                                    color: Colors.white38),
                                onSelected: (v) {
                                  if (v == 'delete') _delete(s);
                                },
                                itemBuilder: (_) => const [
                                  PopupMenuItem(
                                      value: 'delete', child: Text('Delete')),
                                ],
                              ),
                            ),
                          if (rows.isEmpty)
                            Padding(
                              padding: const EdgeInsets.all(24),
                              child: Center(
                                child: Text(
                                  state.sessions.isEmpty
                                      ? 'No conversations yet'
                                      : 'No matches for "$_filter"',
                                  style:
                                      const TextStyle(color: Colors.white38),
                                ),
                              ),
                            ),
                        ],
                      ),
                    ),
            ),
            Divider(color: Colors.white12, height: 1),
            _CreditStrip(state: state),
            ListTile(
              leading: CircleAvatar(
                backgroundColor: const Color(0xFF2E7D32),
                child: Text(
                  state.greetingName.substring(0, 1).toUpperCase(),
                  style: const TextStyle(fontWeight: FontWeight.bold),
                ),
              ),
              title: Text(state.displayName?.isNotEmpty == true
                      ? state.displayName!
                      : (state.email ?? 'Signed in'),
                  maxLines: 1, overflow: TextOverflow.ellipsis),
              subtitle: state.displayName?.isNotEmpty == true
                  ? Text(state.email ?? '',
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(
                          color: Colors.white38, fontSize: 11.5))
                  : null,
              trailing: Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  IconButton(
                    icon: const Icon(Icons.settings_outlined,
                        color: Colors.white54),
                    tooltip: 'Settings',
                    onPressed: () {
                      Navigator.of(context).pop();
                      Navigator.of(context).pushNamed('/settings');
                    },
                  ),
                  IconButton(
                    icon: const Icon(Icons.logout_rounded, color: Colors.white54),
                    tooltip: 'Sign out',
                    onPressed: () => state.signOut(),
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
        padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 10),
        child: Row(
          children: [
            const Icon(Icons.monetization_on_outlined,
                size: 18, color: Color(0xFFFFC107)),
            const SizedBox(width: 8),
            Text(
              c != null ? '${c.total} credits' : 'Credits',
              style: const TextStyle(
                  color: Colors.white70, fontWeight: FontWeight.w600),
            ),
            const Spacer(),
            const Icon(Icons.chevron_right_rounded, color: Colors.white38),
          ],
        ),
      ),
    );
  }
}