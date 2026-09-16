import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_markdown/flutter_markdown.dart';
import 'package:mime/mime.dart' as mime_lib;
import 'package:open_filex/open_filex.dart';
import 'package:path_provider/path_provider.dart';
import 'package:provider/provider.dart';
import 'package:url_launcher/url_launcher.dart';
import 'package:wakelock_plus/wakelock_plus.dart';

import '../config.dart';
import '../models.dart';
import '../services/chat_cache.dart';
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
  Timer? _flushTimer;

  ChatMessage? _liveTurn; // assistant bubble for the current (or resumed) turn

  // Liveness tracking. A turn is ONLY cleared by an authoritative terminal
  // event (turn_end / goal_status idle / final message). The previous build
  // force-cleared the busy state after 90 s of "silence", which cut long,
  // legitimately quiet tool runs short. Now we distinguish:
  //   * socket healthy but quiet  → the task is simply still working;
  //   * socket down               → reconnect is already retrying, so keep the
  //                                 turn alive and resync when it returns.
  DateTime _lastEventAt = DateTime.now();
  Timer? _resyncTimer;
  bool _resyncInFlight = false;
  Timer? _stopWatchTimer; // bounded grace window after a /stop request
  bool _justCompleted = false; // shows the settled "done" check in the pill
  Timer? _completedFadeTimer;
  Timer? _cacheTimer; // debounced local transcript persistence

  /// How long a /stop request may stay unacknowledged before the UI releases
  /// the composer. The server is authoritative, but a dead socket must not
  /// lock the user out forever.
  static const Duration _stopGrace = Duration(seconds: 20);

  /// Minimum gap between scroll-to-bottom animations while streaming. Starting
  /// a new 180 ms animation on every delta (many per second) is what made the
  /// chat visibly flicker up and down.
  static const Duration _scrollThrottle = Duration(milliseconds: 260);
  DateTime _lastAutoScrollAt = DateTime.fromMillisecondsSinceEpoch(0);

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
      // Android suspends the isolate while backgrounded, which commonly leaves
      // a HALF-OPEN socket: isConnected still reports true but writes vanish
      // and no error ever arrives — the chat simply looks "paused" forever.
      // Verify liveness first (forcing a clean reconnect when stale) and then
      // reconcile with the server.
      final state = context.read<AppState>();
      state.loadSessions();
      state.refreshCredits();
      unawaited(_recoverOnResume());
    }
  }

  Future<void> _recoverOnResume() async {
    final chatId = _chatId;
    if (chatId == null) return;
    try {
      final sock = await context.read<AppState>().ensureSocket();
      _socket = sock;
      _wireSocket(sock);
      // Rebuild the connection when it is stale, then re-subscribe.
      await sock.checkLiveness();
      await sock.attach(chatId);
      if (mounted) setState(() => _connected = sock.isConnected);
      _registerView();
    } catch (_) {
      if (mounted) setState(() => _connected = false);
    }
    await _resync();
  }

  /// Re-establish the socket + subscription and reconcile the transcript with
  /// the server. Called on resume and whenever the socket comes back, so a
  /// turn that ran while the screen was closed reappears (with its answer).
  Future<void> _resync() async {
    final chatId = _chatId;
    if (chatId == null || _resyncInFlight) return;
    _resyncInFlight = true;
    try {
      final state = context.read<AppState>();
      final sock = await state.ensureSocket();
      _socket = sock;
      _wireSocket(sock);
      _registerView();
      await sock.attach(chatId);
      if (!mounted) return;
      setState(() => _connected = sock.isConnected);

      // The gateway does not replay accumulated deltas, so pull the thread to
      // recover anything produced while this screen was not listening. This
      // also clears a stale busy pill when the turn finished in the background.
      final session = widget.session;
      if (session != null) {
        final history = await state.openSession(session);
        if (!mounted) return;
        _applyServerHistory(history);
      }
    } catch (_) {
      // Offline: keep the transcript we have and let the socket retry.
    } finally {
      _resyncInFlight = false;
    }
  }

  Future<void> _boot() async {
    final state = context.read<AppState>();
    if (widget.session != null) {
      final session = widget.session!;
      _chatId = session.chatId;
      state.rememberChat(session.chatId);
      // Render the local copy FIRST so the screen never flashes empty and
      // prior answers are visible instantly, then reconcile in background.
      final cached = await state.chatCache.load(session.chatId);
      if (mounted && cached.isNotEmpty) {
        setState(() {
          _messages
            ..clear()
            ..addAll(cached);
        });
      } else {
        setState(() => _loadingHistory = true);
      }
      try {
        final history = await state.openSession(session);
        if (mounted) _applyServerHistory(history);
      } catch (e) {
        if (mounted) {
          _toast('Could not load history: $e');
        }
      }
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

  /// Install server history without losing anything that was streamed locally.
  ///
  /// The previous implementation DELETED the persisted assistant bubble of the
  /// active turn because it assumed the server replays that turn's deltas on
  /// attach. It does not (only goal_state/goal_status), so text disappeared.
  /// We now merge instead, which also repairs answers truncated by app close.
  void _applyServerHistory(ThreadHistory history) {
    final merged = mergeThreadHistory(
      server: history.messages,
      cached: List<ChatMessage>.from(_messages),
    );
    setState(() {
      _messages
        ..clear()
        ..addAll(merged);
      if (history.activeTurnId != null) {
        _remoteRunning = true;
        _lastEventAt = DateTime.now();
      }
    });
    _scheduleCacheWrite();
    if (history.activeTurnId != null) {
      _startResyncWatch();
    }
  }

  void _wireSocket(NanobotSocket sock) {
    sock.onGoalStatus = (chatId, status) {
      if (!mounted || chatId != _chatId) return;
      _touchActivity();
      final running = status == 'running';
      setState(() => _remoteRunning = running);
      if (running) _ensureLiveTurn(); // replayed running turn → open bubble
      _updateWakelock();
    };
    sock.onConnectionChanged = (connected) {
      if (!mounted) return;
      setState(() => _connected = connected);
      if (connected) {
        // The socket came back: re-attach and reconcile so a turn that ran
        // while we were disconnected is reflected (and never left "paused").
        unawaited(_resync());
      }
    };
    sock.onTurnActivity = (chatId) {
      if (chatId == _chatId) _touchActivity();
    };
    sock.onErrorEvent = (chatId, detail) {
      if (!mounted) return;
      if (chatId == null || chatId == _chatId) _toast('Error: $detail');
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
      setState(() => _connected = sock.isConnected);
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
        _touchActivity();
        _ensureLiveTurn().appendDelta(chunk);
        _scheduleFlush();
        _scrollToBottom();
      },
      onReasoningDelta: (chunk) {
        _touchActivity();
        final t = _ensureLiveTurn();
        t.reasoning += chunk;
        t.reasoningStreaming = true;
        _scheduleFlush();
        _scrollToBottom();
      },
      onReasoningEnd: () {
        final t = _liveTurn;
        if (t != null) t.reasoningStreaming = false;
        if (mounted) setState(() {});
      },
      onStreamEnd: (finalText) {
        _touchActivity();
        final t = _liveTurn;
        if (t != null) {
          t.endSegment(finalText);
          t.dropEmptyTrailingSegment();
        }
        if (mounted) setState(() {});
        _scrollToBottom();
      },
      onActivity: (steps) {
        _touchActivity();
        final t = _ensureLiveTurn();
        for (final s in steps) {
          _upsertStep(t, s);
        }
        _scheduleFlush();
        _scrollToBottom();
      },
      onFinalMessage: (text, media) {
        _touchActivity();
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
        _touchActivity();
        // Terminal for the turn: ALWAYS terminate the busy state, even when
        // no live bubble exists (e.g. the turn ran while the screen was
        // closed). Skipping this is what let the green indicator keep
        // rolling after the task already completed.
        final t = _liveTurn;
        if (t != null) {
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
        }
        _cancelStopFallback();
        if (mounted) {
          setState(() {
            _sending = false;
            _stopping = false;
            _remoteRunning = false;
            _lastEventAt = DateTime.now();
          });
          _updateWakelock();
          _flashCompleted();
          _scrollToBottom();
          _scheduleCacheWrite();
        }
      },
      onError: (detail) {
        _touchActivity();
        final t = _ensureLiveTurn();
        t.streaming = false;
        t.hasError = true;
        if (t.text.isEmpty) {
          t.segments.add('⚠️ $detail');
        }
        _liveTurn = null;
        _cancelStopFallback();
        if (mounted) {
          setState(() {
            _sending = false;
            _stopping = false;
            _remoteRunning = false;
          });
          _updateWakelock();
          _scheduleCacheWrite();
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
      onUsage: (usage) {
        if (usage == null) return;
        // Attach replay: seed the running turn's footer so credits/usage stay
        // visible immediately after a resume.
        final t = _liveTurn;
        if (t != null) {
          t.usage = {...?t.usage, ...usage};
          if (mounted) setState(() {});
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

  /// Keep the newest content in view while a turn streams.
  ///
  /// Two guards prevent the "chat shakes up and down" jitter:
  ///  1. Throttle: at most one animation per [_scrollThrottle] window, since
  ///     deltas arrive many times per second and overlapping 180 ms
  ///     animations fought each other.
  ///  2. Only animate when the user is already parked near the bottom — if
  ///     they scrolled up to read, the view must not be yanked back.
  void _scrollToBottom({bool force = false}) {
    if (!force) {
      final now = DateTime.now();
      if (now.difference(_lastAutoScrollAt) < _scrollThrottle) return;
    }
    _lastAutoScrollAt = DateTime.now();
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted || !_scroll.hasClients) return;
      final pos = _scroll.position;
      final nearBottom = pos.maxScrollExtent - pos.pixels < 180;
      if (!force && !nearBottom) return;
      // Clamp to the real extent: animating past it (the old +120 overshoot)
      // produced a rubber-band bounce on every frame.
      final target = pos.maxScrollExtent;
      if ((pos.pixels - target).abs() < 1) return;
      pos.animateTo(
        target,
        duration: const Duration(milliseconds: 160),
        curve: Curves.easeOut,
      );
    });
  }

  /// Keep the screen awake while a turn is running — long tasks (up to ~1h)
  /// must keep streaming with the display on.
  void _updateWakelock() {
    if (_busy) {
      WakelockPlus.enable();
      _startResyncWatch();
    } else {
      WakelockPlus.disable();
      _stopResyncWatch();
      _scheduleCacheWrite();
    }
  }

  /// Coalesces high-frequency streaming deltas into UI rebuilds (~12/s max)
  /// so the markdown bubble re-renders smoothly instead of on every chunk.
  void _scheduleFlush() {
    _flushTimer ??= Timer(const Duration(milliseconds: 80), () {
      _flushTimer = null;
      if (mounted) setState(() {});
      _scheduleCacheWrite();
    });
  }

  // ---- Turn liveness, resync & local persistence -------------------------

  void _touchActivity() {
    _lastEventAt = DateTime.now();
  }

  /// Periodic safety net while a turn is running.
  ///
  /// It NEVER force-clears a running turn (that is what stopped long tasks
  /// early). Its only job is to notice that the socket is gone or that we may
  /// have missed a terminal event while backgrounded, and reconcile with the
  /// server — the authoritative source of turn state.
  void _startResyncWatch() {
    _resyncTimer ??= Timer.periodic(const Duration(seconds: 15), (_) async {
      if (!mounted || !_busy) {
        _stopResyncWatch();
        return;
      }
      final sock = _socket;
      final offline = sock == null || !sock.isConnected;
      // No inbound frame for a while although the socket claims to be up:
      // verify the turn is genuinely still running instead of assuming.
      final stale = DateTime.now().difference(_lastEventAt) >
          const Duration(seconds: 90);
      if (offline || stale) {
        await _resync();
      }
    });
  }

  void _stopResyncWatch() {
    _resyncTimer?.cancel();
    _resyncTimer = null;
  }

  /// Write the transcript to disk (debounced) so reopening the app always
  /// shows what was produced, even for a turn that never completed in-view.
  void _scheduleCacheWrite() {
    final chatId = _chatId;
    if (chatId == null) return;
    _cacheTimer ??= Timer(const Duration(milliseconds: 700), () {
      _cacheTimer = null;
      final state = context.read<AppState>();
      unawaited(state.cacheThread(chatId, List<ChatMessage>.from(_messages)));
    });
  }

  /// Clears the busy state after the server failed to confirm a stop. Only
  /// used for the stop path, where the user explicitly asked to end the turn.
  void _forceClearRunning({String? reason}) {
    if (!mounted) return;
    final hadLive = _liveTurn != null;
    final t = _liveTurn;
    if (t != null) {
      t.streaming = false;
      t.reasoningStreaming = false;
      for (final s in t.activity) {
        if (!s.isDone) s.status = 'done';
      }
      _liveTurn = null;
    }
    _cancelStopFallback();
    _stopResyncWatch();
    setState(() {
      _sending = false;
      _stopping = false;
      _remoteRunning = false;
      _lastEventAt = DateTime.now();
    });
    _updateWakelock();
    if (hadLive) _scrollToBottom();
    if (reason != null) _toast(reason);
  }

  /// Settle the status pill on a STATIC green check after a turn completes.
  /// It fades away after a few seconds — it never keeps spinning.
  void _flashCompleted() {
    _completedFadeTimer?.cancel();
    if (!mounted) return;
    setState(() => _justCompleted = true);
    _completedFadeTimer = Timer(const Duration(seconds: 4), () {
      if (mounted) setState(() => _justCompleted = false);
    });
  }

  void _cancelStopFallback() {
    _stopWatchTimer?.cancel();
    _stopWatchTimer = null;
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
      _stopping = false;
      _lastEventAt = DateTime.now();
    });
    _updateWakelock();
    _scrollToBottom(force: true);
    _scheduleCacheWrite();

    try {
      _socket ??= await state.ensureSocket();
      _wireSocket(_socket!);
      if (_chatId == null) {
        _chatId = await _socket!.newChat();
        state.rememberChat(_chatId!);
      }
      _registerView();
      setState(() => _connected = _socket!.isConnected);
    } catch (e) {
      _fail('Connection error: $e');
      return;
    }

    _ensureLiveTurn();
    // Mark the turn before the frame hits the wire so an early terminal event
    // is still attributed to this turn.
    _socket!.markUserTurn(_chatId!);
    _socket!.sendMessage(_chatId!, text,
        media: wireMedia.isEmpty ? null : wireMedia);
  }

  /// Cancel the running task (server `/stop` slash command).
  ///
  /// The server is authoritative: the busy state clears when `goal_status
  /// idle` / `turn_end` arrives. A bounded grace timer only covers a dead
  /// socket, so a stop is never "cut" locally while work is still finishing.
  void _stop() {
    final chatId = _chatId;
    if (chatId == null || _socket == null || !_busy) return;
    _touchActivity();
    setState(() => _stopping = true);
    _socket!.stopTask(chatId);
    _cancelStopFallback();
    _stopWatchTimer = Timer(_stopGrace, () {
      if (mounted && _busy) {
        _forceClearRunning(reason: 'Task cancelled.');
      }
    });
  }

  void _fail(String msg) {
    if (!mounted) return;
    setState(() => _sending = false);
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(msg)));
  }

  /// Resolve the gateway session key for this chat (needed for file access).
  String? _sessionKeyForChat(AppState state) {
    final opened = widget.session;
    if (opened != null && opened.chatId == _chatId) return opened.key;
    for (final s in state.sessions) {
      if (s.chatId == _chatId) return s.key;
    }
    return null;
  }

  /// Download a file the assistant created during this turn and hand it to
  /// the OS viewer.
  ///
  /// The gateway exposes a text preview only (binary files answer 415), so we
  /// surface the REAL reason instead of a generic failure, and never write a
  /// zero-byte file. Files that cannot be previewed are reported clearly with
  /// the path, which the agent can still fetch in-chat.
  Future<void> _openArtifact(String path) async {
    final state = context.read<AppState>();
    final key = _sessionKeyForChat(state);
    if (key == null || state.apiToken == null) {
      _toast('Reopen this chat from Conversations to download its files.');
      return;
    }
    try {
      final payload = await state.api.fetchFilePreview(state.apiToken!, key,
          path: path, supabaseToken: state.accessToken);
      final content = payload['content'];
      if (content is! String || content.isEmpty) {
        throw StateError('file is empty or not text-previewable');
      }
      final name = path.split('/').last;
      final dir = await getTemporaryDirectory();
      final file = File(
          '${dir.path}/${DateTime.now().millisecondsSinceEpoch}-$name');
      await file.writeAsString(content, flush: true);
      final result = await OpenFilex.open(file.path);
      if (result.type != ResultType.done) {
        _toast('Saved to ${file.path} (no viewer for this type).');
      }
    } on ApiException catch (e) {
      if (e.status == 415) {
        _toast('"${path.split('/').last}" is binary — ask the agent to send it '
            'as an attachment to download it.');
      } else if (e.status == 404) {
        _toast('File not found in this workspace: $path');
      } else if (e.status == 403) {
        _toast('That file is outside this chat\'s workspace.');
      } else {
        _toast('Could not download "$path": ${e.message}');
      }
    } catch (e) {
      _toast('Could not download "$path": $e');
    }
  }

  void _toast(String msg) {
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(msg)));
  }

  @override
  void dispose() {
    _flushTimer?.cancel();
    _resyncTimer?.cancel();
    _stopWatchTimer?.cancel();
    _completedFadeTimer?.cancel();
    _cacheTimer?.cancel();
    // Persist the final transcript before leaving so reopening the chat shows
    // the completed answer immediately.
    final chatId = _chatId;
    if (chatId != null) {
      unawaited(context
          .read<AppState>()
          .cacheThread(chatId, List<ChatMessage>.from(_messages)));
    }
    WakelockPlus.disable();
    WidgetsBinding.instance.removeObserver(this);
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
            // Fixed-size status slot: occupies the same space whether it is
            // showing the working spinner, the settled done check, or nothing,
            // so the title never shifts and the pill never bounces.
            Positioned(
              right: 0,
              child: _StatusPill(
                  busy: _busy, stopping: _stopping, completed: _justCompleted),
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
          if (!_connected)
            Container(
              width: double.infinity,
              color: const Color(0xFF3E2723),
              padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 7),
              child: const Row(
                children: [
                  Icon(Icons.cloud_off_rounded,
                      size: 15, color: Colors.orangeAccent),
                  SizedBox(width: 8),
                  Expanded(
                    child: Text(
                      'Reconnecting… your task keeps running on the server.',
                      style: TextStyle(fontSize: 12, color: Colors.orangeAccent),
                    ),
                  ),
                ],
              ),
            ),
          Expanded(
            child: _loadingHistory
                ? const Center(child: CircularProgressIndicator())
                : ListView.builder(
                    controller: _scroll,
                    padding: const EdgeInsets.fromLTRB(14, 12, 14, 12),
                    itemCount: _messages.length,
                    itemBuilder: (_, i) => _Bubble(
                        message: _messages[i],
                        onOpenArtifact: (p) => _openArtifact(p)),
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
  const _Bubble({required this.message, this.onOpenArtifact});
  final ChatMessage message;
  final void Function(String path)? onOpenArtifact;

  @override
  Widget build(BuildContext context) {
    final isUser = message.role == Role.user;
    final bubble = Container(
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
      child: _content(context, isUser),
    );
    return Align(
      alignment: isUser ? Alignment.centerRight : Alignment.centerLeft,
      // Long-press copies the message text — handy for short answers and
      // error reports on a phone.
      child: GestureDetector(
        onLongPress: message.text.trim().isEmpty
            ? null
            : () {
                Clipboard.setData(ClipboardData(text: message.text));
                ScaffoldMessenger.of(context).showSnackBar(const SnackBar(
                    content: Text('Copied to clipboard'),
                    duration: Duration(seconds: 1)));
              },
        child: bubble,
      ),
    );
  }

  Widget _content(BuildContext context, bool isUser) {
    return isUser
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
                  if (message.artifactPaths.isNotEmpty)
                    Padding(
                      padding: const EdgeInsets.only(top: 8),
                      child: _FileChips(
                          paths: message.artifactPaths,
                          onOpen: onOpenArtifact),
                    ),
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
                  if (message.streaming &&
                      message.isEmpty &&
                      message.activity.isEmpty)
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

/// Fixed-size app-bar status slot. It occupies the same space whether it is
/// showing the working spinner, the settled done check, or nothing, so the
/// title never shifts and the indicator never bounces. The completion state
/// is a STATIC check — it settles instead of continuing to spin.
class _StatusPill extends StatelessWidget {
  const _StatusPill({
    required this.busy,
    required this.stopping,
    required this.completed,
  });

  final bool busy;
  final bool stopping;
  final bool completed;

  @override
  Widget build(BuildContext context) {
    Widget? content;
    if (busy) {
      content = _pill(
        const SizedBox(
            width: 10,
            height: 10,
            child: CircularProgressIndicator(
                strokeWidth: 1.6, color: Color(0xFF66BB6A))),
        stopping ? 'stopping…' : 'working…',
      );
    } else if (completed) {
      // Static, non-animating confirmation that the task completed.
      content = _pill(
        const Icon(Icons.check_circle, size: 13, color: Color(0xFF66BB6A)),
        'done',
      );
    }
    return SizedBox(
      width: 104,
      height: 24,
      child: AnimatedSwitcher(
        duration: const Duration(milliseconds: 200),
        child: content ?? const SizedBox.shrink(key: ValueKey('empty')),
      ),
    );
  }

  Widget _pill(Widget leading, String label) {
    return Container(
      key: ValueKey(label),
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
      decoration: BoxDecoration(
        color: const Color(0xFF1A2138),
        borderRadius: BorderRadius.circular(10),
      ),
      child: Row(mainAxisSize: MainAxisSize.min, children: [
        leading,
        const SizedBox(width: 6),
        Text(label,
            style: const TextStyle(fontSize: 11, color: Colors.white70)),
      ]),
    );
  }
}

/// File artifacts the assistant created during the turn, shown as chips with
/// a download affordance. Tapping one fetches the file through the gateway
/// and hands it to the OS viewer.
class _FileChips extends StatelessWidget {
  const _FileChips({required this.paths, required this.onOpen});
  final List<String> paths;
  final void Function(String path)? onOpen;

  @override
  Widget build(BuildContext context) {
    return Wrap(
      spacing: 6,
      runSpacing: 6,
      children: [
        for (final p in paths)
          InkWell(
            borderRadius: BorderRadius.circular(8),
            onTap: onOpen == null ? null : () => onOpen!(p),
            child: Container(
              padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 5),
              decoration: BoxDecoration(
                color: Colors.black.withValues(alpha: 0.35),
                borderRadius: BorderRadius.circular(8),
                border: Border.all(color: Colors.white12),
              ),
              child: Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  const Icon(Icons.insert_drive_file_outlined,
                      size: 14, color: Color(0xFF66BB6A)),
                  const SizedBox(width: 5),
                  Text(_fileBaseName(p),
                      style:
                          const TextStyle(color: Colors.white, fontSize: 12)),
                  const SizedBox(width: 4),
                  const Icon(Icons.download_rounded,
                      size: 14, color: Colors.white54),
                ],
              ),
            ),
          ),
      ],
    );
  }

  static String _fileBaseName(String path) {
    final clean = path.split('?').first;
    final segs = clean.split('/');
    return segs.last.isEmpty ? clean : segs.last;
  }
}
