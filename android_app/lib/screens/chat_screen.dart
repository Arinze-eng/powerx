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

class _ChatScreenState extends State<ChatScreen> {
  final TextEditingController _input = TextEditingController();
  final ScrollController _scroll = ScrollController();
  final List<ChatMessage> _messages = [];
  final List<PendingAttachment> _pending = [];

  String? _chatId;
  NanobotSocket? _socket;
  bool _sending = false;
  bool _loadingHistory = false;
  /// True while a server-side turn is running that we are merely observing
  /// (e.g. resumed after the app was closed) — disables composer send.
  bool _remoteRunning = false;
  final OnlyFilesUploader _uploader = OnlyFilesUploader();

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) => _boot());
  }

  Future<void> _boot() async {
    final state = context.read<AppState>();
    if (widget.session != null) {
      setState(() => _loadingHistory = true);
      try {
        final turns = await state.openSession(widget.session!);
        _chatId = widget.session!.chatId;
        state.rememberChat(_chatId!);
        for (final t in turns) {
          _messages.add(ChatMessage(
            id: DateTime.now().microsecondsSinceEpoch.toString(),
            role: t.role == 'user' ? Role.user : Role.assistant,
            text: t.content,
            reasoning: t.reasoning ?? '',
            media: [...t.media],
          ));
        }
      } catch (_) {}
      if (mounted) setState(() => _loadingHistory = false);
      // Attach so any still-running background turn streams into this view.
      await _attachAndWatch();
    } else {
      // Provision a fresh chat immediately so typing feels instant.
      try {
        final sock = await state.ensureSocket();
        _socket = sock;
        final id = await sock.newChat();
        _chatId = id;
        state.rememberChat(id);
        _wireGoalStatus(sock);
      } catch (_) {}
    }
  }

  /// Re-attach to an existing chat and subscribe to live events. If the agent
  /// is still working on a backgrounded turn, goal_status/turn events resume.
  Future<void> _attachAndWatch() async {
    if (_chatId == null) return;
    final state = context.read<AppState>();
    try {
      final sock = await state.ensureSocket();
      _socket = sock;
      _wireGoalStatus(sock);
      await sock.attach(_chatId!);
    } catch (_) {}
  }

  void _wireGoalStatus(NanobotSocket sock) {
    sock.onGoalStatus = (chatId, status) {
      if (!mounted || chatId != _chatId) return;
      final running = status == 'running';
      if (running) {
        // A remote/background turn is active. Ensure we have a streaming bubble
        // and a passive observer registered so its events render live.
        final hasStreaming = _messages.any((m) => m.streaming);
        if (!hasStreaming) {
          final assistant = ChatMessage(
              id: 'r-${DateTime.now().microsecondsSinceEpoch}',
              role: Role.assistant,
              streaming: true);
          setState(() {
            _remoteRunning = true;
            _messages.add(assistant);
          });
          _registerObserverFor(sock, chatId, assistant);
        } else {
          setState(() => _remoteRunning = true);
        }
      } else {
        setState(() => _remoteRunning = false);
      }
    };
  }

  /// Register a passive observer that streams a backgrounded turn into [target].
  void _registerObserverFor(
      NanobotSocket sock, String chatId, ChatMessage assistant) {
    sock.observe(
      chatId,
      onDelta: (chunk) {
        assistant.text += chunk;
        if (mounted) setState(() {});
        _scrollToBottom();
      },
      onActivity: (step) {
        _upsertStep(assistant, step);
        if (mounted) setState(() {});
        _scrollToBottom();
      },
      onDone: (full, media) {
        assistant.text = full.isNotEmpty ? full : assistant.text;
        assistant.streaming = false;
        for (final s in assistant.activity) {
          if (!s.isDone) s.status = 'done';
        }
        if (media.isNotEmpty) {
          assistant.media = [
            ...assistant.media,
            ...media.where((m) => !assistant.media.contains(m)),
          ];
        }
        if (mounted) {
          setState(() {
            _remoteRunning = false;
            _sending = false;
          });
          _scrollToBottom();
        }
      },
      onError: (detail) {
        assistant.streaming = false;
        assistant.hasError = true;
        if (assistant.text.isEmpty) assistant.text = '⚠️ $detail';
        if (mounted) {
          setState(() {
            _remoteRunning = false;
            _sending = false;
          });
        }
      },
    );
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
        withData: true, // load bytes so images can be base64-encoded
      );
      if (result == null || result.files.isEmpty) return;
      for (final f in result.files) {
        final path = f.path;
        if (path == null) continue;
        final size = f.size;
        final kind = _kindFor(f.name, path);
        final att = PendingAttachment(
          id: 'att-${DateTime.now().microsecondsSinceEpoch}-${f.name}',
          name: f.name,
          kind: kind,
          sizeBytes: size,
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
        // Images ride inline as base64 data URLs (matches WebUI behaviour).
        final bytes = await file.readAsBytes();
        if (bytes.lengthInBytes > 8 * 1024 * 1024) {
          throw StateError('Image too large (max ~8 MB).');
        }
        final mt = mime_lib.lookupMimeType(att.localPath ?? '') ?? 'image/png';
        att.dataUrl = 'data:$mt;base64,${base64Encode(bytes)}';
      } else {
        // Videos & arbitrary files upload straight to onlyfiles.com.
        att.url = await _uploader.upload(file, name: att.name);
      }
      att.status = 'ready';
    } catch (e) {
      att.status = 'error';
      att.errorText = '$e';
    }
    if (mounted) setState(() {});
  }

  void _removeAttachment(PendingAttachment att) {
    setState(() => _pending.remove(att));
  }

  // ---- Sending ----------------------------------------------------------

  Future<void> _send() async {
    final text = _input.text.trim();
    final readyMedia = _pending.where((a) => a.isReady).toList();
    if ((text.isEmpty && readyMedia.isEmpty) || _sending) return;
    if (_pending.any((a) => a.status == 'uploading')) {
      _toast('Wait for attachments to finish uploading.');
      return;
    }
    FocusScope.of(context).unfocus();
    _input.clear();

    final state = context.read<AppState>();
    final wireMedia = readyMedia.map((a) => a.toWireMedia()).toList();

    setState(() {
      // Finalize any stale streaming/observer bubble before starting a new turn.
      for (final m in _messages) {
        if (m.streaming) {
          m.streaming = false;
          for (final s in m.activity) {
            if (!s.isDone) s.status = 'done';
          }
        }
      }
      _remoteRunning = false;
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
      _chatId ??= await _socket!.newChat();
      state.rememberChat(_chatId!);
    } catch (e) {
      _fail('Connection error: $e');
      return;
    }

    final assistant = ChatMessage(
        id: 'a-${DateTime.now().microsecondsSinceEpoch}',
        role: Role.assistant,
        streaming: true);
    if (mounted) setState(() => _messages.add(assistant));
    _scrollToBottom();

    _socket!.sendMessage(
      _chatId!,
      text,
      media: wireMedia.isEmpty ? null : wireMedia,
      onDelta: (chunk) {
        assistant.text += chunk;
        if (mounted) setState(() {});
        _scrollToBottom();
      },
      onActivity: (step) {
        _upsertStep(assistant, step);
        if (mounted) setState(() {});
        _scrollToBottom();
      },
      onDone: (full, media) {
        assistant.text = full.isNotEmpty ? full : assistant.text;
        assistant.streaming = false;
        assistant.media = [
          ...assistant.media,
          ...media.where((m) => !assistant.media.contains(m)),
        ];
        for (final s in assistant.activity) {
          if (!s.isDone) s.status = 'done';
        }
        if (mounted) {
          setState(() => _sending = false);
          _scrollToBottom();
        }
        // Refresh sidebar sessions so titles/previews update.
        unawaited(state.loadSessions());
      },
      onError: (detail) {
        assistant.streaming = false;
        assistant.hasError = true;
        if (assistant.text.isEmpty) assistant.text = '⚠️ $detail';
        if (mounted) {
          setState(() => _sending = false);
          _scrollToBottom();
        }
      },
    );
  }

  void _upsertStep(ChatMessage msg, ActivityStep incoming) {
    final idx = msg.activity.indexWhere((s) => s.id == incoming.id);
    if (idx >= 0) {
      // A later phase for the same call_id updates status (running -> done/error).
      msg.activity[idx].status = incoming.status;
    } else {
      msg.activity.add(incoming);
    }
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
    _input.dispose();
    _scroll.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final showTyping = (_sending || _remoteRunning) && !_hasStreaming;
    return Scaffold(
      backgroundColor: const Color(0xFF0B1020),
      appBar: AppBar(
        backgroundColor: const Color(0xFF0B1020),
        elevation: 0,
        title: const Text('PowerX', style: TextStyle(fontWeight: FontWeight.w800)),
        centerTitle: true,
        actions: [
          IconButton(
            icon: const Icon(Icons.settings_outlined),
            tooltip: 'Settings',
            onPressed: () => Navigator.of(context)
                .pushNamed('/settings'),
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
                    itemCount: _messages.length + (showTyping ? 1 : 0),
                    itemBuilder: (_, i) {
                      if (i >= _messages.length) return const _TypingDots();
                      return _Bubble(message: _messages[i]);
                    },
                  ),
          ),
          _Composer(
            controller: _input,
            sending: _sending || _remoteRunning,
            pending: _pending,
            onPick: _pickFiles,
            onRemove: _removeAttachment,
            onSend: _send,
          ),
        ],
      ),
    );
  }

  bool get _hasStreaming => _messages.any((m) => m.streaming);
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
            BoxConstraints(maxWidth: MediaQuery.of(context).size.width * 0.86),
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
                        style: const TextStyle(color: Colors.white, fontSize: 15)),
                  if (message.media.isNotEmpty)
                    Padding(
                      padding: EdgeInsets.only(top: message.text.isNotEmpty ? 8 : 0),
                      child: Wrap(
                        spacing: 6,
                        runSpacing: 6,
                        alignment: WrapAlignment.end,
                        children: [
                          for (final m in message.media)
                            _AttachmentChip(label: _basename(m), url: m),
                        ],
                      ),
                    ),
                ],
              )
            : Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  if (message.activity.isNotEmpty)
                    _ActivityPanel(steps: message.activity),
                  MarkdownBody(
                    data: message.text.isEmpty && message.streaming
                        ? (message.activity.isEmpty ? '…' : '')
                        : message.text,
                    selectable: true,
                    styleSheet: MarkdownStyleSheet.fromTheme(Theme.of(context))
                        .copyWith(
                      p: const TextStyle(
                          color: Colors.white, fontSize: 15, height: 1.35),
                      codeblockDecoration: BoxDecoration(
                          color: Colors.black.withValues(alpha: 0.35),
                          borderRadius: BorderRadius.circular(8)),
                    ),
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
                  if (message.streaming &&
                      message.text.isEmpty &&
                      message.activity.isEmpty)
                    const Padding(
                      padding: EdgeInsets.only(top: 6),
                      child: _TypingDots(compact: true),
                    ),
                ],
              ),
      ),
    );
  }

  static String _basename(String path) {
    final clean = path.split('?').first;
    final segs = clean.split('/');
    return segs.last.isEmpty ? clean : segs.last;
  }
}

/// Renders the ordered list of tool/activity steps for a turn, collapsing
/// completed ones behind a summary line like the WebUI's activity timeline.
class _ActivityPanel extends StatelessWidget {
  const _ActivityPanel({required this.steps});
  final List<ActivityStep> steps;

  @override
  Widget build(BuildContext context) {
    final running = steps.where((s) => !s.isDone).length;
    final done = steps.length - running;
    return Container(
      margin: const EdgeInsets.only(bottom: 8),
      padding: const EdgeInsets.fromLTRB(10, 8, 10, 8),
      decoration: BoxDecoration(
        color: Colors.black.withValues(alpha: 0.22),
        borderRadius: BorderRadius.circular(10),
        border: Border.all(color: Colors.white12),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        mainAxisSize: MainAxisSize.min,
        children: [
          Row(
            children: [
              Icon(
                running > 0 ? Icons.autorenew : Icons.check_circle_outline,
                size: 15,
                color: running > 0 ? const Color(0xFF66BB6A) : Colors.white54,
              ),
              const SizedBox(width: 6),
              Text(
                running > 0 ? 'Working · $done/$steps.length steps' : 'Completed · ${steps.length} steps',
                style: const TextStyle(
                    color: Colors.white70,
                    fontSize: 12,
                    fontWeight: FontWeight.w600),
              ),
            ],
          ),
          const SizedBox(height: 6),
          for (final s in steps) _StepRow(step: s),
        ],
      ),
    );
  }
}

class _StepRow extends StatelessWidget {
  const _StepRow({required this.step});
  final ActivityStep step;

  IconData get _icon {
    switch (step.iconKey) {
      case 'read':
        return Icons.description_outlined;
      case 'write':
        return Icons.edit_note_rounded;
      case 'search':
        return Icons.travel_explore_outlined;
      case 'run':
        return Icons.terminal_rounded;
      case 'image':
        return Icons.image_outlined;
      case 'list':
        return Icons.format_list_bulleted_rounded;
      default:
        return Icons.build_outlined;
    }
  }

  @override
  Widget build(BuildContext context) {
    final label = step.detail.isNotEmpty ? '${step.name} · ${step.detail}' : step.name;
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 2),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Icon(_icon, size: 14, color: Colors.white54),
          const SizedBox(width: 8),
          Expanded(
            child: Text(
              label,
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: const TextStyle(color: Colors.white70, fontSize: 12.5),
            ),
          ),
          const SizedBox(width: 6),
          _StepStatus(status: step.status),
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
      return const SizedBox(
        width: 12,
        height: 12,
        child: CircularProgressIndicator(strokeWidth: 1.6, color: Color(0xFF66BB6A)),
      );
    }
    if (status == 'error') {
      return const Icon(Icons.error_outline, size: 14, color: Colors.redAccent);
    }
    return const Icon(Icons.check, size: 14, color: Color(0xFF66BB6A));
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
            loadingBuilder: (_, child, prog) =>
                prog == null ? child : const SizedBox(
                    width: 120, height: 120, child: Center(child: CircularProgressIndicator(strokeWidth: 2))),
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
    required this.sending,
    required this.pending,
    required this.onPick,
    required this.onRemove,
    required this.onSend,
  });
  final TextEditingController controller;
  final bool sending;
  final List<PendingAttachment> pending;
  final VoidCallback onPick;
  final void Function(PendingAttachment) onRemove;
  final VoidCallback onSend;

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
                      hintText: sending ? 'PowerX is working…' : 'Message PowerX…',
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
                Material(
                  color: const Color(0xFF2E7D32),
                  shape: const CircleBorder(),
                  child: InkWell(
                    customBorder: const CircleBorder(),
                    onTap: sending ? null : onSend,
                    child: Padding(
                      padding: const EdgeInsets.all(12),
                      child: Icon(Icons.arrow_upward_rounded,
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
