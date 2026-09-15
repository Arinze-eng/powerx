import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart';
import 'package:flutter_markdown/flutter_markdown.dart';
import 'package:mime/mime.dart' as mime_lib;
import 'package:open_filex/open_filex.dart';
import 'package:provider/provider.dart';
import 'package:url_launcher/url_launcher.dart';

import '../config.dart';
import '../models.dart';
import '../services/gateway_api.dart';
import '../services/nanobot_socket.dart';
import '../state/app_state.dart';

class ChatScreen extends StatefulWidget {
  const ChatScreen({super.key, this.session});
  final SessionSummary? session;

  @override
  State<ChatScreen> createState() => _ChatScreenState();
}

class _ChatScreenState extends State<ChatScreen> with WidgetsBindingObserver {
  final TextEditingController _input = TextEditingController();
  final ScrollController _scroll = ScrollController();
  final List<ChatMessage> _messages = [];
  final List<PendingAttachment> _pending = [];

  String? _chatId;
  NanobotSocket? _socket;
  ChatView? _view;
  bool _sending = false; // local send in flight
  bool _remoteRunning = false; // server reports an active turn
  bool _stopping = false;
  bool _loadingHistory = false;
  bool _connected = false;
  final OnlyFilesUploader _uploader = OnlyFilesUploader();

  ChatMessage? _liveTurn; // assistant bubble for the current (or resumed) turn

  bool get _busy => _sending || _remoteRunning;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    WidgetsBinding.instance.addPostFrameCallback((_) => _boot());
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState s) {
    if (s == AppLifecycleState.resumed) {
      // The socket auto-reconnects; make sure the token is hot and re-pull
      // the turn state so a backgrounded task resumes streaming here.
      final state = context.read<AppState>();
      state.loadSessions();
      state.refreshCredits();
      unawaited(_reattach());
    }
  }

  Future<void> _reattach() async {
    if (_chatId == null) return;
    try {
      final sock = await context.read<AppState>().ensureSocket();
      _socket = sock;
      await sock.attach(_chatId!);
    } catch (_) {}
  }

  Future<void> _boot() async {
    final state = context.read<AppState>();
    if (widget.session != null) {
      setState(() => _loadingHistory = true);
      try {
        final history = await state.openSession(widget.session!);
        _chatId = widget.session!.chatId;
        state.rememberChat(_chatId!);
        _messages.addAll(history.messages);
        if (history.activeTurnId != null) {
          // The server replays the whole in-flight turn's events after attach
          // (hydrate-after-subscribe). Drop the persisted partials so the
          // replay rebuilds the bubble without duplicated text.
          _messages.removeWhere((m) =>
              m.role == Role.assistant && m.turnId == history.activeTurnId);
          _remoteRunning = true;
        }
      } catch (_) {}
      if (mounted) setState(() => _loadingHistory = false);
      await _attachAndWatch();
    } else {
      try {
        final sock = await state.ensureSocket();
        _socket = sock;
        _wireSocket(sock);
        final id = await sock.newChat();
        _chatId = id;
        state.rememberChat(id);
        _registerView();
        setState(() => _connected = true);
      } catch (e) {
        _toast('Connection error: $e');
      }
    }
  }

  void _wireSocket(NanobotSocket sock) {
    sock.onGoalStatus = (chatId, status) {
      if (!mounted || chatId != _chatId) return;
      final running = status == 'running';
      setState(() => _remoteRunning = running);
      if (running) _ensureLiveTurn(); // replayed running turn → open bubble
    };
  }

  /// Re-attach to an existing chat and subscribe to live events. If the agent
  /// is still working on a backgrounded turn, goal_status/turn events resume.
  Future<void> _attachAndWatch() async {
    if (_chatId == null) return;
    final state = context.read<AppState>();
    try {
      final sock = await state.ensureSocket();
      _socket = sock;
      _wireSocket(sock);
      _registerView();
      await sock.attach(_chatId!);
      setState(() => _connected = true);
    } catch (e) {
      if (mounted) setState(() => _connected = false);
    }
  }

  /// Lazily create the assistant bubble that receives live turn events.
  ChatMessage _ensureLiveTurn() {
    final existing = _liveTurn;
    if (existing != null) return existing;
    final msg = ChatMessage(
        id: 'live-${DateTime.now().microsecondsSinceEpoch}',
        role: Role.assistant,
        streaming: true);
    setState(() {
      _messages.add(msg);
      _liveTurn = msg;
    });
    _scrollToBottom();
    return msg;
  }

  /// Build the ChatView (reads `_liveTurn` lazily per event) and install it.
  void _registerView() {
    final chatId = _chatId;
    if (chatId == null || _socket == null) return;
    _view = ChatView(
      onDelta: (chunk) {
        _ensureLiveTurn().appendDelta(chunk);
        if (mounted) setState(() {});
        _scrollToBottom();
      },
      onReasoningDelta: (chunk) {
        final t = _ensureLiveTurn();
        t.reasoning += chunk;
        t.reasoningStreaming = true;
        if (mounted) setState(() {});
        _scrollToBottom();
      },
      onReasoningEnd: () {
        final t = _liveTurn;
        if (t != null) t.reasoningStreaming = false;
        if (mounted) setState(() {});
      },
      onStreamEnd: (finalText) {
        final t = _liveTurn;
        if (t != null) {
          t.endSegment(finalText);
          t.dropEmptyTrailingSegment();
        }
        if (mounted) setState(() {});
        _scrollToBottom();
      },
      onActivity: (steps) {
        final t = _ensureLiveTurn();
        for (final s in steps) {
          _upsertStep(t, s);
        }
        if (mounted) setState(() {});
        _scrollToBottom();
      },
      onFinalMessage: (text, media) {
        // Authoritative complete answer. Absorb into the live turn if one
        // exists, else surface as a standalone bubble (e.g. the "/stop"
        // acknowledgement that arrives after goal_status idle).
        var turn = _liveTurn;
        if (turn == null) {
          if (text.trim().isEmpty && media.isEmpty) return;
          turn = ChatMessage(
              id: 'm-${DateTime.now().microsecondsSinceEpoch}',
              role: Role.assistant,
              streaming: false);
          _messages.add(turn);
        }
        final t = turn;
        if (text.trim().isNotEmpty && text.length >= t.text.length) {
          t.segments
            ..clear()
            ..add(text);
        }
        if (media.isNotEmpty) {
          t.media = [
            ...t.media,
            ...media.where((m) => !t.media.contains(m)),
          ];
        }
        if (mounted) setState(() {});
      },
      onTurnEnd: (summary) {
        final t = _liveTurn;
        if (t == null) return;
        t.streaming = false;
        t.reasoningStreaming = false;
        t.dropEmptyTrailingSegment();
        t.usage = summary.usage ?? t.usage;
        t.latencyMs = summary.latencyMs ?? t.latencyMs;
        if (summary.media.isNotEmpty) {
          t.media = {...t.media, ...summary.media}.toList();
        }
        for (final s in t.activity) {
          if (!s.isDone) s.status = 'done';
        }
        _liveTurn = null;
        if (mounted) {
          setState(() {
            _sending = false;
            _stopping = false;
            _remoteRunning = false;
          });
          _scrollToBottom();
        }
      },
      onError: (detail) {
        final t = _ensureLiveTurn();
        t.streaming = false;
        t.hasError = true;
        if (t.text.isEmpty) {
          t.segments.add('⚠️ $detail');
        }
        _liveTurn = null;
        if (mounted) {
          setState(() {
            _sending = false;
            _stopping = false;
            _remoteRunning = false;
          });
        }
      },
      onUserMessage: (text, turnId) {
        // Projected user echo / replay after reconnect — dedupe by turnId
        // when known, else by identical user text in this view.
        final dup = _messages.any((m) =>
            m.role == Role.user &&
            ((turnId != null && m.turnId == turnId) ||
                (turnId == null && m.text == text)));
        if (!dup && text.trim().isNotEmpty) {
          setState(() => _messages.add(ChatMessage(
              id: 'u-echo-${DateTime.now().microsecondsSinceEpoch}',
              role: Role.user,
              text: text,
              turnId: turnId)));
          _scrollToBottom();
        }
      },
    );
    _socket!.listen(chatId, _view!);
  }

  void _upsertStep(ChatMessage msg, ActivityStep incoming) {
    final idx = msg.activity.indexWhere((s) => s.id == incoming.id);
    if (idx >= 0) {
      msg.activity[idx].status = incoming.status;
    } else {
      msg.activity.add(incoming);
    }
  }

  void _scrollToBottom() {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (_scroll.hasClients) {
        _scroll.animateTo(
          _scroll.position.maxScrollExtent + 120,
          duration: const Duration(milliseconds: 200),
          curve: Curves.easeOut,
        );
      }
    });
  }

  // ---- File / image attachment -----------------------------------------

  Future<void> _pickFiles() async {
    try {
      final result = await FilePicker.platform.pickFiles(
        allowMultiple: true,
        type: FileType.any,
        withData: true,
      );
      if (result == null || result.files.isEmpty) return;
      for (final f in result.files) {
        final path = f.path;
        if (path == null) continue;
        final kind = _kindFor(f.name, path);
        final att = PendingAttachment(
          id: 'att-${DateTime.now().microsecondsSinceEpoch}-${f.name}',
          name: f.name,
          kind: kind,
          sizeBytes: f.size,
          localPath: path,
          status: 'uploading',
        );
        setState(() => _pending.add(att));
        unawaited(_prepareAttachment(att, File(path)));
      }
    } catch (e) {
      _toast('Could not pick files: $e');
    }
  }

  String _kindFor(String name, String path) {
    final mt = mime_lib.lookupMimeType(path) ?? '';
    if (mt.startsWith('image/')) return 'image';
    if (mt.startsWith('video/')) return 'video';
    return 'file';
  }

  Future<void> _prepareAttachment(PendingAttachment att, File file) async {
    try {
      if (att.kind == 'image') {
        final bytes = await file.readAsBytes();
        if (bytes.lengthInBytes > 8 * 1024 * 1024) {
          throw StateError('Image too large (max ~8 MB).');
        }
        final mt = mime_lib.lookupMimeType(att.localPath ?? '') ?? 'image/png';
        att.dataUrl = 'data:$mt;base64,${base64Encode(bytes)}';
      } else {
        att.url = await _uploader.upload(file, name: att.name);
      }
      att.status = 'ready';
    } catch (e) {
      att.status = 'error';
      att.errorText = '$e';
    }
    if (mounted) setState(() {});
  }

  // ---- Sending / stopping ------------------------------------------------

  Future<void> _send() async {
    final text = _input.text.trim();
    final readyMedia = _pending.where((a) => a.isReady).toList();
    if ((text.isEmpty && readyMedia.isEmpty) || _busy) return;
    if (_pending.any((a) => a.status == 'uploading')) {
      _toast('Wait for attachments to finish uploading.');
      return;
    }
    FocusScope.of(context).unfocus();
    _input.clear();

    final state = context.read<AppState>();
    final wireMedia = readyMedia.map((a) => a.toWireMedia()).toList();

    setState(() {
      _messages.add(ChatMessage(
          id: 'u-${DateTime.now().microsecondsSinceEpoch}',
          role: Role.user,
          text: text,
          media: readyMedia
              .map((a) => a.url ?? (a.localPath ?? ''))
              .where((s) => s.isNotEmpty)
              .toList()));
      _pending.clear();
      _sending = true;
    });
    _scrollToBottom();

    try {
      _socket ??= await state.ensureSocket();
      _wireSocket(_socket!);
      _chatId ??= await _socket!.newChat();
      state.rememberChat(_chatId!);
      _registerView();
    } catch (e) {
      _fail('Connection error: $e');
      return;
    }

    _ensureLiveTurn();
    _socket!.sendMessage(_chatId!, text,
        media: wireMedia.isEmpty ? null : wireMedia);
  }

  /// Cancel the running task (server `/stop` slash command).
  void _stop() {
    final chatId = _chatId;
    if (chatId == null || _socket == null || !_busy) return;
    setState(() => _stopping = true);
    _socket!.stopTask(chatId);
  }

  void _fail(String msg) {
    if (!mounted) return;
    setState(() => _sending = false);
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(msg)));
  }

  void _toast(String msg) {
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(msg)));
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    final chatId = _chatId;
    if (chatId != null) _socket?.unlisten(chatId);
    _input.dispose();
    _scroll.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: const Color(0xFF0B1020),
      appBar: AppBar(
        backgroundColor: const Color(0xFF0B1020),
        elevation: 0,
        title: Stack(
          alignment: Alignment.center,
          children: [
            const Text(PowerXConfig.appName, style: TextStyle(fontWeight: FontWeight.w800)),
            if (_busy)
              Positioned(
                right: 0,
                child: Container(
                  padding:
                      const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
                  decoration: BoxDecoration(
                    color: const Color(0xFF1A2138),
                    borderRadius: BorderRadius.circular(10),
                  ),
                  child: Row(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      const SizedBox(
                          width: 10,
                          height: 10,
                          child: CircularProgressIndicator(
                              strokeWidth: 1.6, color: Color(0xFF66BB6A))),
                      const SizedBox(width: 6),
                      Text(_stopping ? 'stopping…' : 'working…',
                          style: const TextStyle(
                              fontSize: 11, color: Colors.white70)),
                    ],
                  ),
                ),
              ),
          ],
        ),
        centerTitle: true,
        actions: [
          if (!_connected)
            const Padding(
              padding: EdgeInsets.only(right: 4),
              child: Tooltip(
                message: 'Reconnecting…',
                child: Icon(Icons.cloud_off_rounded,
                    size: 18, color: Colors.orangeAccent),
              ),
            ),
          IconButton(
            icon: const Icon(Icons.settings_outlined),
            tooltip: 'Settings',
            onPressed: () => Navigator.of(context).pushNamed('/settings'),
          ),
        ],
      ),
      body: Column(
        children: [
          Expanded(
            child: _loadingHistory
                ? const Center(child: CircularProgressIndicator())
                : ListView.builder(
                    controller: _scroll,
                    padding: const EdgeInsets.fromLTRB(14, 12, 14, 12),
                    itemCount: _messages.length,
                    itemBuilder: (_, i) => _Bubble(message: _messages[i]),
                  ),
          ),
          _Composer(
            controller: _input,
            busy: _busy,
            stopping: _stopping,
            pending: _pending,
            onPick: _pickFiles,
            onRemove: (a) => setState(() => _pending.remove(a)),
            onSend: _send,
            onStop: _stop,
          ),
        ],
      ),
    );
  }
}

class _Bubble extends StatelessWidget {
  const _Bubble({required this.message});
  final ChatMessage message;

  @override
  Widget build(BuildContext context) {
    final isUser = message.role == Role.user;
    return Align(
      alignment: isUser ? Alignment.centerRight : Alignment.centerLeft,
      child: Container(
        margin: const EdgeInsets.symmetric(vertical: 6),
        constraints:
            BoxConstraints(maxWidth: MediaQuery.of(context).size.width * 0.90),
        decoration: BoxDecoration(
          color: isUser ? const Color(0xFF2E7D32) : const Color(0xFF1A2138),
          borderRadius: BorderRadius.only(
            topLeft: const Radius.circular(18),
            topRight: const Radius.circular(18),
            bottomLeft: Radius.circular(isUser ? 18 : 6),
            bottomRight: Radius.circular(isUser ? 6 : 18),
          ),
        ),
        padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
        child: isUser
            ? Column(
                crossAxisAlignment: CrossAxisAlignment.end,
                mainAxisSize: MainAxisSize.min,
                children: [
                  if (message.text.isNotEmpty)
                    SelectableText(message.text,
                        style: const TextStyle(
                            color: Colors.white, fontSize: 15)),
                  if (message.media.isNotEmpty)
                    Padding(
                      padding: EdgeInsets.only(
                          top: message.text.isNotEmpty ? 8 : 0),
                      child: Wrap(
                        spacing: 6,
                        runSpacing: 6,
                        alignment: WrapAlignment.end,
                        children: [
                          for (final m in message.media)
                            _AttachmentChip(
                                label: _basename(m), url: m),
                        ],
                      ),
                    ),
                ],
              )
            : Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                mainAxisSize: MainAxisSize.min,
                children: [
                  if (message.activity.isNotEmpty)
                    _ActivityPanel(
                        steps: message.activity, turnStreaming: message.streaming),
                  if (message.reasoning.trim().isNotEmpty)
                    _ThinkingPanel(
                        reasoning: message.reasoning,
                        streaming: message.reasoningStreaming),
                  for (var i = 0; i < message.segments.length; i++)
                    Padding(
                      padding: EdgeInsets.only(
                          top: i == 0 ? 0 : 8),
                      child: MarkdownBody(
                        data: message.segments[i],
                        selectable: true,
                        styleSheet:
                            MarkdownStyleSheet.fromTheme(Theme.of(context))
                                .copyWith(
                          p: const TextStyle(
                              color: Colors.white,
                              fontSize: 15,
                              height: 1.35),
                          codeblockDecoration: BoxDecoration(
                              color: Colors.black.withValues(alpha: 0.35),
                              borderRadius: BorderRadius.circular(8)),
                        ),
                      ),
                    ),
                  if (message.streaming && message.isEmpty)
                    const Padding(
                      padding: EdgeInsets.only(top: 2),
                      child: _TypingDots(compact: true),
                    ),
                  if (message.viewableMedia.isNotEmpty)
                    Padding(
                      padding: const EdgeInsets.only(top: 8),
                      child: Wrap(
                        spacing: 8,
                        runSpacing: 8,
                        children: [
                          for (final u in message.viewableMedia)
                            _MediaLink(url: u),
                        ],
                      ),
                    ),
                  if (!message.streaming && message.hasError)
                    const Padding(
                      padding: EdgeInsets.only(top: 6),
                      child: Icon(Icons.error_outline,
                          size: 16, color: Colors.redAccent),
                    ),
                  if (!message.streaming && _footer(message) != null)
                    Padding(
                      padding: const EdgeInsets.only(top: 6),
                      child: Text(_footer(message)!,
                          style: const TextStyle(
                              color: Colors.white30, fontSize: 11)),
                    ),
                ],
              ),
      ),
    );
  }

  static String? _footer(ChatMessage m) {
    final parts = <String>[];
    final calls = m.usage?['llm_calls'];
    if (calls != null && calls > 0) parts.add('API calls: ${calls.toInt()}');
    if (m.latencyMs != null && m.latencyMs! > 0) {
      parts.add('${(m.latencyMs! / 1000).toStringAsFixed(1)}s');
    }
    return parts.isEmpty ? null : parts.join(' · ');
  }

  static String _basename(String path) {
    final clean = path.split('?').first;
    final segs = clean.split('/');
    return segs.last.isEmpty ? clean : segs.last;
  }
}

/// Ordered list of tool/activity steps for a turn, live-updated.
class _ActivityPanel extends StatelessWidget {
  const _ActivityPanel({required this.steps, required this.turnStreaming});
  final List<ActivityStep> steps;
  final bool turnStreaming;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 8),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        mainAxisSize: MainAxisSize.min,
        children: [
          for (final s in steps)
            Padding(
              padding: const EdgeInsets.symmetric(vertical: 3),
              child: Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  _StepStatus(status: s.status),
                  const SizedBox(width: 8),
                  Expanded(
                    child: Text(
                      s.detail.isNotEmpty
                          ? '${s.name} · ${s.detail}'
                          : s.name,
                      style: TextStyle(
                        color: s.isDone ? Colors.white54 : Colors.white70,
                        fontSize: 12.5,
                        decoration: TextDecoration.none,
                      ),
                    ),
                  ),
                ],
              ),
            ),
        ],
      ),
    );
  }
}

class _StepStatus extends StatelessWidget {
  const _StepStatus({required this.status});
  final String status;
  @override
  Widget build(BuildContext context) {
    if (status == 'running') {
      return const Padding(
        padding: EdgeInsets.only(top: 2),
        child: SizedBox(
          width: 12,
          height: 12,
          child: CircularProgressIndicator(
              strokeWidth: 1.6, color: Color(0xFF66BB6A)),
        ),
      );
    }
    if (status == 'error') {
      return const Icon(Icons.error_outline, size: 14, color: Colors.redAccent);
    }
    return const Icon(Icons.check_circle_outline,
        size: 14, color: Color(0xFF4CAF50));
  }
}

/// Collapsible "Thinking" panel showing the model's reasoning stream,
/// expanded automatically while it is still streaming.
class _ThinkingPanel extends StatefulWidget {
  const _ThinkingPanel({required this.reasoning, required this.streaming});
  final String reasoning;
  final bool streaming;

  @override
  State<_ThinkingPanel> createState() => _ThinkingPanelState();
}

class _ThinkingPanelState extends State<_ThinkingPanel> {
  bool? _override; // null = auto (follow streaming state)

  @override
  void didUpdateWidget(covariant _ThinkingPanel old) {
    super.didUpdateWidget(old);
    if (old.streaming && !widget.streaming) {
      // Collapse automatically when thinking ends (unless user opened it).
      if (_override == null) setState(() => _override = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final expanded = _override ?? widget.streaming;
    return Container(
      margin: const EdgeInsets.only(bottom: 8),
      decoration: BoxDecoration(
        color: Colors.black.withValues(alpha: 0.22),
        borderRadius: BorderRadius.circular(10),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          InkWell(
            onTap: () => setState(() => _override = !expanded),
            child: Padding(
              padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 7),
              child: Row(
                children: [
                  Icon(
                    expanded
                        ? Icons.expand_more_rounded
                        : Icons.chevron_right_rounded,
                    size: 18,
                    color: Colors.white54,
                  ),
                  const SizedBox(width: 4),
                  Text(
                    widget.streaming ? 'Thinking…' : 'Thought',
                    style: TextStyle(
                      color: widget.streaming
                          ? const Color(0xFF9CCC65)
                          : Colors.white54,
                      fontSize: 12.5,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                  const Spacer(),
                  const Icon(Icons.psychology_alt_outlined,
                      size: 14, color: Colors.white24),
                ],
              ),
            ),
          ),
          if (expanded)
            Padding(
              padding: const EdgeInsets.fromLTRB(10, 0, 10, 8),
              child: SelectableText(
                widget.reasoning.length > 4000
                    ? '${widget.reasoning.substring(0, 4000)}…'
                    : widget.reasoning,
                style: const TextStyle(
                    color: Colors.white38, fontSize: 12, height: 1.35),
              ),
            ),
        ],
      ),
    );
  }
}

class _AttachmentChip extends StatelessWidget {
  const _AttachmentChip({required this.label, required this.url});
  final String label;
  final String url;
  @override
  Widget build(BuildContext context) {
    final isHttp = url.startsWith('http');
    return InkWell(
      onTap: isHttp ? () => _launch(url) : null,
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
        decoration: BoxDecoration(
          color: Colors.black.withValues(alpha: 0.2),
          borderRadius: BorderRadius.circular(8),
        ),
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            const Icon(Icons.attach_file, size: 14, color: Colors.white70),
            const SizedBox(width: 6),
            ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 160),
              child: Text(label,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: const TextStyle(color: Colors.white70, fontSize: 12)),
            ),
          ],
        ),
      ),
    );
  }
}

class _MediaLink extends StatelessWidget {
  const _MediaLink({required this.url});
  final String url;
  @override
  Widget build(BuildContext context) {
    final isImage = _looksLikeImage(url);
    if (isImage) {
      return ClipRRect(
        borderRadius: BorderRadius.circular(10),
        child: InkWell(
          onTap: () => _launch(url),
          child: Image.network(
            url,
            errorBuilder: (_, __, ___) =>
                _AttachmentChip(label: _basename(url), url: url),
            loadingBuilder: (_, child, prog) => prog == null
                ? child
                : const SizedBox(
                    width: 120,
                    height: 120,
                    child:
                        Center(child: CircularProgressIndicator(strokeWidth: 2))),
            fit: BoxFit.cover,
            width: 220,
            height: 160,
          ),
        ),
      );
    }
    return _AttachmentChip(label: _basename(url), url: url);
  }

  static bool _looksLikeImage(String u) {
    final l = u.toLowerCase().split('?').first;
    return l.endsWith('.png') ||
        l.endsWith('.jpg') ||
        l.endsWith('.jpeg') ||
        l.endsWith('.gif') ||
        l.endsWith('.webp');
  }

  static String _basename(String path) {
    final clean = path.split('?').first;
    final segs = clean.split('/');
    return segs.last.isEmpty ? clean : segs.last;
  }
}

Future<void> _launch(String url) async {
  final uri = Uri.tryParse(url);
  if (uri == null) return;
  try {
    if (!await launchUrl(uri, mode: LaunchMode.externalApplication)) {
      await OpenFilex.open(url);
    }
  } catch (_) {
    await OpenFilex.open(url);
  }
}

class _TypingDots extends StatefulWidget {
  const _TypingDots({this.compact = false});
  final bool compact;
  @override
  State<_TypingDots> createState() => _TypingDotsState();
}

class _TypingDotsState extends State<_TypingDots>
    with SingleTickerProviderStateMixin {
  late final AnimationController _c = AnimationController(
      vsync: this, duration: const Duration(milliseconds: 900))
    ..repeat();
  @override
  void dispose() {
    _c.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return AnimatedBuilder(
      animation: _c,
      builder: (_, __) {
        return Row(
          mainAxisSize: MainAxisSize.min,
          children: List.generate(3, (i) {
            final v = (_c.value * 3 - i).clamp(0.0, 1.0);
            return Container(
              width: widget.compact ? 6 : 8,
              height: widget.compact ? 6 : 8,
              margin: const EdgeInsets.symmetric(horizontal: 2),
              decoration: BoxDecoration(
                color: Colors.white.withValues(alpha: 0.3 + 0.5 * v),
                shape: BoxShape.circle,
              ),
            );
          }),
        );
      },
    );
  }
}

class _Composer extends StatelessWidget {
  const _Composer({
    required this.controller,
    required this.busy,
    required this.stopping,
    required this.pending,
    required this.onPick,
    required this.onRemove,
    required this.onSend,
    required this.onStop,
  });
  final TextEditingController controller;
  final bool busy;
  final bool stopping;
  final List<PendingAttachment> pending;
  final VoidCallback onPick;
  final void Function(PendingAttachment) onRemove;
  final VoidCallback onSend;
  final VoidCallback onStop;

  @override
  Widget build(BuildContext context) {
    return SafeArea(
      top: false,
      child: Container(
        padding: const EdgeInsets.fromLTRB(12, 8, 12, 12),
        decoration: const BoxDecoration(
          color: Color(0xFF0B1020),
          border: Border(top: BorderSide(color: Colors.white10)),
        ),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            if (pending.isNotEmpty)
              Padding(
                padding: const EdgeInsets.only(bottom: 8),
                child: SizedBox(
                  height: 64,
                  child: ListView(
                    scrollDirection: Axis.horizontal,
                    children: [
                      for (final a in pending)
                        Padding(
                          padding: const EdgeInsets.only(right: 8),
                          child: _PendingTile(
                              attachment: a, onRemove: () => onRemove(a)),
                        ),
                    ],
                  ),
                ),
              ),
            Row(
              crossAxisAlignment: CrossAxisAlignment.end,
              children: [
                IconButton(
                  onPressed: onPick,
                  icon: const Icon(Icons.attach_file_rounded,
                      color: Colors.white54),
                  tooltip: 'Attach files',
                ),
                Expanded(
                  child: TextField(
                    controller: controller,
                    minLines: 1,
                    maxLines: 5,
                    textInputAction: TextInputAction.newline,
                    style: const TextStyle(color: Colors.white, fontSize: 15),
                    decoration: InputDecoration(
                      hintText: busy
                          ? (stopping ? 'Stopping task…' : 'CDNAI is working…')
                          : 'Message CDNAI…',
                      hintStyle: const TextStyle(color: Colors.white38),
                      filled: true,
                      fillColor: const Color(0xFF1A2138),
                      contentPadding: const EdgeInsets.symmetric(
                          horizontal: 16, vertical: 12),
                      border: OutlineInputBorder(
                          borderRadius: BorderRadius.circular(22),
                          borderSide: BorderSide.none),
                    ),
                  ),
                ),
                const SizedBox(width: 8),
                // Send arrow when idle; red stop square while the agent works.
                Material(
                  color: busy ? Colors.red.shade700 : const Color(0xFF2E7D32),
                  shape: const CircleBorder(),
                  child: InkWell(
                    customBorder: const CircleBorder(),
                    onTap: busy ? (stopping ? null : onStop) : onSend,
                    child: Padding(
                      padding: const EdgeInsets.all(12),
                      child: busy
                          ? (stopping
                              ? const SizedBox(
                                  width: 18,
                                  height: 18,
                                  child: CircularProgressIndicator(
                                      strokeWidth: 2, color: Colors.white))
                              : const Icon(Icons.stop_rounded,
                                  color: Colors.white, size: 22))
                          : const Icon(Icons.arrow_upward_rounded,
                              color: Colors.white, size: 22),
                    ),
                  ),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}

class _PendingTile extends StatelessWidget {
  const _PendingTile({required this.attachment, required this.onRemove});
  final PendingAttachment attachment;
  final VoidCallback onRemove;

  @override
  Widget build(BuildContext context) {
    final isImage = attachment.kind == 'image' && attachment.localPath != null;
    return Stack(
      clipBehavior: Clip.none,
      children: [
        Container(
          width: 64,
          height: 64,
          decoration: BoxDecoration(
            color: const Color(0xFF1A2138),
            borderRadius: BorderRadius.circular(10),
            border: Border.all(
                color: attachment.isError ? Colors.redAccent : Colors.white12),
          ),
          clipBehavior: Clip.antiAlias,
          child: isImage
              ? Image.file(File(attachment.localPath!), fit: BoxFit.cover)
              : Center(
                  child: Column(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      Icon(
                        attachment.kind == 'video'
                            ? Icons.movie_outlined
                            : Icons.insert_drive_file_outlined,
                        size: 22,
                        color: Colors.white54,
                      ),
                      const SizedBox(height: 2),
                      Text(
                        _shortName(attachment.name),
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(
                            color: Colors.white54, fontSize: 9),
                      ),
                    ],
                  ),
                ),
        ),
        if (attachment.status == 'uploading')
          Positioned.fill(
            child: ColoredBox(
              color: Colors.black45,
              child: const Center(
                child: SizedBox(
                    width: 18,
                    height: 18,
                    child: CircularProgressIndicator(
                        strokeWidth: 2, color: Colors.white)),
              ),
            ),
          ),
        if (attachment.isError)
          Positioned.fill(
            child: Tooltip(
              message: attachment.errorText ?? 'Upload failed',
              child: const ColoredBox(
                color: Colors.black54,
                child: Center(
                    child: Icon(Icons.error_outline,
                        color: Colors.redAccent, size: 22)),
              ),
            ),
          ),
        Positioned(
          top: -6,
          right: -6,
          child: GestureDetector(
            onTap: onRemove,
            child: Container(
              decoration: const BoxDecoration(
                  color: Color(0xFF2E7D32), shape: BoxShape.circle),
              padding: const EdgeInsets.all(2),
              child: const Icon(Icons.close, size: 12, color: Colors.white),
            ),
          ),
        ),
      ],
    );
  }

  String _shortName(String n) {
    if (n.length <= 8) return n;
    return '${n.substring(0, 5)}…';
  }
}
