import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_markdown/flutter_markdown.dart';
import 'package:provider/provider.dart';

import '../models.dart';
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

  String? _chatId;
  NanobotSocket? _socket;
  bool _sending = false;
  bool _loadingHistory = false;

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
    } else {
      // Provision a fresh chat immediately so typing feels instant.
      try {
        final sock = await state.ensureSocket();
        _socket = sock;
        final id = await sock.newChat();
        _chatId = id;
      } catch (_) {}
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

  Future<void> _send() async {
    final text = _input.text.trim();
    if (text.isEmpty || _sending) return;
    FocusScope.of(context).unfocus();
    _input.clear();

    final state = context.read<AppState>();
    setState(() {
      _messages.add(ChatMessage(id: 'u-${DateTime.now().microsecondsSinceEpoch}',
          role: Role.user, text: text));
      _sending = true;
    });
    _scrollToBottom();

    try {
      _socket ??= await state.ensureSocket();
      _chatId ??= await _socket!.newChat();
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
      onDelta: (chunk) {
        assistant.text += chunk;
        if (mounted) setState(() {});
        _scrollToBottom();
      },
      onDone: (full, media) {
        assistant.text = full.isNotEmpty ? full : assistant.text;
        assistant.streaming = false;
        assistant.media = media;
        if (mounted) {
          setState(() => _sending = false);
          _scrollToBottom();
        }
      },
      onError: (detail) {
        assistant.streaming = false;
        if (assistant.text.isEmpty) assistant.text = '⚠️ $detail';
        if (mounted) {
          setState(() => _sending = false);
          _scrollToBottom();
        }
      },
    );
  }

  void _fail(String msg) {
    if (!mounted) return;
    setState(() => _sending = false);
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
    return Scaffold(
      backgroundColor: const Color(0xFF0B1020),
      appBar: AppBar(
        backgroundColor: const Color(0xFF0B1020),
        elevation: 0,
        title: const Text('PowerX', style: TextStyle(fontWeight: FontWeight.w800)),
        centerTitle: true,
      ),
      body: Column(
        children: [
          Expanded(
            child: _loadingHistory
                ? const Center(child: CircularProgressIndicator())
                : ListView.builder(
                    controller: _scroll,
                    padding: const EdgeInsets.fromLTRB(14, 12, 14, 12),
                    itemCount: _messages.length + (_sending && !_hasStreaming ? 1 : 0),
                    itemBuilder: (_, i) {
                      if (i >= _messages.length) return const _TypingDots();
                      return _Bubble(message: _messages[i]);
                    },
                  ),
          ),
          _Composer(
            controller: _input,
            sending: _sending,
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
        constraints: BoxConstraints(
            maxWidth: MediaQuery.of(context).size.width * 0.82),
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
            ? SelectableText(message.text,
                style: const TextStyle(color: Colors.white, fontSize: 15))
            : Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  MarkdownBody(
                    data: message.text.isEmpty && message.streaming
                        ? '…'
                        : message.text,
                    selectable: true,
                    styleSheet: MarkdownStyleSheet.fromTheme(Theme.of(context))
                        .copyWith(
                      p: const TextStyle(color: Colors.white, fontSize: 15, height: 1.35),
                      codeblockDecoration: BoxDecoration(
                          color: Colors.black.withValues(alpha: 0.35),
                          borderRadius: BorderRadius.circular(8)),
                    ),
                  ),
                  if (message.streaming && message.text.isEmpty)
                    const Padding(
                      padding: EdgeInsets.only(top: 6),
                      child: _TypingDots(compact: true),
                    ),
                ],
              ),
      ),
    );
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
  const _Composer(
      {required this.controller, required this.sending, required this.onSend});
  final TextEditingController controller;
  final bool sending;
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
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.end,
          children: [
            Expanded(
              child: TextField(
                controller: controller,
                minLines: 1,
                maxLines: 5,
                textInputAction: TextInputAction.newline,
                style: const TextStyle(color: Colors.white, fontSize: 15),
                decoration: InputDecoration(
                  hintText: 'Message PowerX…',
                  hintStyle: const TextStyle(color: Colors.white38),
                  filled: true,
                  fillColor: const Color(0xFF1A2138),
                  contentPadding:
                      const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
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
      ),
    );
  }
}
