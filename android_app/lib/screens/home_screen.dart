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
              onNew: _newChat),
        ],
      ),
    );
  }
}

/// Landing view shown when no conversation is open.
class _ChatLanding extends StatelessWidget {
  const _ChatLanding({required this.onMenu, required this.onNew});
  final VoidCallback onMenu;
  final VoidCallback onNew;

  @override
  Widget build(BuildContext context) {
    final state = context.watch<AppState>();
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
            child: Center(
              child: Padding(
                padding: const EdgeInsets.all(28),
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Container(
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
                    const SizedBox(height: 20),
                    Text('Hi ${state.greetingName} 👋',
                        style: const TextStyle(
                            fontSize: 22, fontWeight: FontWeight.w700)),
                    const SizedBox(height: 8),
                    const Text(
                      'How can I help you today?',
                      textAlign: TextAlign.center,
                      style: TextStyle(color: Colors.white54, fontSize: 15),
                    ),
                    const SizedBox(height: 28),
                    FilledButton.icon(
                      onPressed: onNew,
                      icon: const Icon(Icons.add_comment_outlined),
                      label: const Text('Start a new chat'),
                      style: FilledButton.styleFrom(
                        backgroundColor: const Color(0xFF2E7D32),
                        minimumSize: const Size(200, 50),
                      ),
                    ),
                  ],
                ),
              ),
            ),
          ),
        ],
      ),
    );
  }
}

class _SessionsDrawer extends StatelessWidget {
  const _SessionsDrawer(
      {required this.state, required this.onNew, required this.onOpen});
  final AppState state;
  final VoidCallback onNew;
  final void Function(SessionSummary) onOpen;

  @override
  Widget build(BuildContext context) {
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
              child: SizedBox(
                width: double.infinity,
                child: FilledButton.icon(
                  onPressed: onNew,
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
                          for (final s in state.sessions)
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
                              onTap: () => onOpen(s),
                              trailing: _SessionMenu(state: state, session: s),
                            ),
                          if (state.sessions.isEmpty)
                            const Padding(
                              padding: EdgeInsets.all(24),
                              child: Center(
                                child: Text('No conversations yet',
                                    style: TextStyle(color: Colors.white38)),
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
              title: Text(state.displayName ?? state.email ?? 'Signed in',
                  maxLines: 1, overflow: TextOverflow.ellipsis),
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

class _SessionMenu extends StatelessWidget {
  const _SessionMenu({required this.state, required this.session});
  final AppState state;
  final SessionSummary session;

  @override
  Widget build(BuildContext context) {
    return PopupMenuButton<String>(
      icon: const Icon(Icons.more_vert_rounded, color: Colors.white38),
      onSelected: (v) async {
        if (v == 'delete') {
          final ok = await showDialog<bool>(
            context: context,
            builder: (_) => AlertDialog(
              backgroundColor: const Color(0xFF141B33),
              title: const Text('Delete conversation?'),
              content: const Text('This cannot be undone.'),
              actions: [
                TextButton(
                    onPressed: () => Navigator.pop(context, false),
                    child: const Text('Cancel')),
                TextButton(
                    onPressed: () => Navigator.pop(context, true),
                    child: const Text('Delete',
                        style: TextStyle(color: Colors.redAccent))),
              ],
            ),
          );
          if (ok == true) {
            await state.deleteSession(session);
          }
        }
      },
      itemBuilder: (_) => const [
        PopupMenuItem(value: 'delete', child: Text('Delete')),
      ],
    );
  }
}
