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
import 'package:permission_handler/permission_handler.dart';
import 'package:provider/provider.dart';
import 'package:record/record.dart';
import 'package:url_launcher/url_launcher.dart';
import 'package:wakelock_plus/wakelock_plus.dart';

import '../models.dart';
import '../utils/turn_fence.dart';
import '../utils/turn_id.dart';
import '../services/chat_cache.dart';
import '../services/gateway_api.dart';
import '../services/nanobot_socket.dart';
import '../state/app_state.dart';
import '../theme/app_theme.dart';
import '../theme/palette.dart';
import '../widgets/brand.dart';
import '../widgets/theme_toggle.dart';
import '../widgets/latex_markdown.dart';

class ChatScreen extends StatefulWidget {
  const ChatScreen({super.key, this.session, this.initialPrompt});
  final SessionSummary? session;

  /// Optional text seeded into the composer (used by the landing screen's
  /// starter cards so a suggestion opens a ready-to-send draft).
  final String? initialPrompt;

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
  /// A forced reconcile arrived while another pass was running. That pass may
  /// have read the transcript before the answer was persisted, so one more
  /// pass runs as soon as it finishes (see [_resync]).
  bool _resyncQueued = false;
  /// Turn id of the most recent send. Hydration is satisfied only once the
  /// server holds an answer for THIS turn; an older answer does not count.
  String? _pendingTurnId;
  /// Short post-completion reconcile window (see [_armSettleWatch]).
  Timer? _settleWatch;

  /// Which `active turn` a canonical transcript snapshot may believe. The
  /// gateway replays a killed run's wall-clock on every attach, so the
  /// transcript can claim a turn that no longer exists; see [TurnFence].
  final TurnFence _fence = TurnFence();
  /// Canonical-transcript hydration retry (see [_startHydration]).
  Timer? _hydrateTimer;
  int _hydrateAttempt = 0;
  bool _hydrateSatisfied = false;
  Timer? _cacheTimer; // debounced local transcript persistence

  // ---- Voice notes -------------------------------------------------------

  /// Records to a temporary m4a file, then hands the bytes to the gateway for
  /// transcription. The transcript is appended to the composer so the user can
  /// review (and edit) it before sending, which is what "the voice note should
  /// become text in the typing section" asks for.
  late final AudioRecorder _recorder = AudioRecorder();
  bool _recording = false;
  bool _transcribing = false;
  DateTime? _recordStartedAt;
  Timer? _recordTick;
  int _recordSeconds = 0;

  bool get _voiceBusy => _recording || _transcribing;

  /// Start or stop a voice note. While recording, the composer shows a live
  /// level/timer strip; on stop the clip is transcribed into the text field.
  Future<void> _toggleVoiceNote() async {
    if (_transcribing) return;
    if (_recording) {
      await _finishVoiceNote();
    } else {
      await _startVoiceNote();
    }
  }

  Future<void> _startVoiceNote() async {
    if (_voiceBusy) return;
    // The mic permission is granted at runtime (declared in the manifest).
    // Without this the record plugin throws and the button looks dead.
    try {
      final status = await Permission.microphone.request();
      if (!status.isGranted) {
        _toast('Microphone permission is needed for voice notes.');
        return;
      }
    } catch (_) {
      // Permission handler is unavailable on some builds; fall through and let
      // the recorder surface a real error if it truly cannot capture.
    }
    try {
      if (await _recorder.hasPermission() == false) {
        _toast('Microphone permission is needed for voice notes.');
        return;
      }
      final dir = await getTemporaryDirectory();
      final path =
          '${dir.path}/voice-${DateTime.now().microsecondsSinceEpoch}.m4a';
      await _recorder.start(
         RecordConfig(
          // m4a/AAC is in the gateway's allowed audio MIME list and is
          // universally supported by Android encoders.
          encoder: AudioEncoder.aacLc,
          bitRate: 64000,
          sampleRate: 16000,
          numChannels: 1,
        ),
        path: path,
      );
      _recordStartedAt = DateTime.now();
      _recordSeconds = 0;
      _recordTick?.cancel();
      _recordTick = Timer.periodic(const Duration(seconds: 1), (_) {
        if (!mounted) return;
        setState(() => _recordSeconds++);
      });
      if (mounted) {
        setState(() => _recording = true);
      }
    } catch (e) {
      if (mounted) setState(() => _recording = false);
      _toast('Could not start recording: $e');
    }
  }

  Future<void> _finishVoiceNote() async {
    if (!_recording) return;
    _recordTick?.cancel();
    _recordTick = null;
    final started = _recordStartedAt;
    final elapsedMs = started == null
        ? 0
        : DateTime.now().difference(started).inMilliseconds;
    String? path;
    try {
      path = await _recorder.stop();
    } catch (e) {
      if (mounted) setState(() => _recording = false);
      _toast('Could not finish recording: $e');
      return;
    }
    if (mounted) setState(() => _recording = false);

    // A tap-and-release accident produces a clip with no usable audio; the
    // gateway would reject it as "empty". Guard locally for a clear message.
    if (path == null || elapsedMs < 700) {
      if (path != null) unawaited(_deleteQuietly(path));
      _toast('That was too short — hold the mic and speak.');
      return;
    }

    if (mounted) setState(() => _transcribing = true);
    try {
      final file = File(path);
      final bytes = await file.readAsBytes();
      if (bytes.lengthInBytes > 24 * 1024 * 1024) {
        _toast('That voice note is too large to upload.');
        return;
      }
      final dataUrl = 'data:audio/m4a;base64,${base64Encode(bytes)}';
      // Resolve the socket BEFORE the await gap so no BuildContext is used
      // after an async suspension.
      var sock = _socket;
      if (sock == null) {
        if (!mounted) return;
        final state = context.read<AppState>();
        sock = await state.ensureSocket();
        _socket = sock;
      }
      final text = await sock.transcribeAudio(
        dataUrl: dataUrl,
        durationMs: elapsedMs,
      );
      if (!mounted) return;
      final trimmed = text.trim();
      if (trimmed.isEmpty) {
        _toast('No speech detected — try again closer to the mic.');
        return;
      }
      // Append into the composer so the user can review before sending.
      final existing = _input.text.trimRight();
      _input.text = existing.isEmpty ? trimmed : '$existing $trimmed';
      _input.selection = TextSelection.fromPosition(
        TextPosition(offset: _input.text.length),
      );
    } catch (e) {
      final msg = e is StateError ? e.message : '$e';
      _toast(msg);
    } finally {
      if (mounted) setState(() => _transcribing = false);
      unawaited(_deleteQuietly(path));
    }
  }

  /// Remove a temporary clip without ever surfacing a filesystem error.
  static Future<void> _deleteQuietly(String? path) async {
    if (path == null) return;
    try {
      final f = File(path);
      if (await f.exists()) await f.delete();
    } catch (_) {
      // A leftover temp file is harmless; the OS clears the cache dir.
    }
  }

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
    // Seed a starter suggestion so the user can review before sending.
    final seed = widget.initialPrompt;
    if (seed != null && seed.trim().isNotEmpty) {
      _input.text = seed.trim();
    }
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
      final state = context.read<AppState>();
      final sock = await state.ensureSocket();
      _socket = sock;
      _wireSocket(sock);
      // Rebuild the connection when it is stale, then re-subscribe.
      await sock.checkLiveness();
      await sock.attach(chatId);
      if (mounted) setState(() => _connected = sock.isConnected);
      _registerView();
      // A task that started before backgrounding is still ours: make sure the
      // socket is watching this chat even if the screen never re-registered.
      sock.setOpenChat(chatId, sessionKey: widget.session?.key);
    } catch (_) {
      if (mounted) setState(() => _connected = false);
    }
    _startHydration();
    await _resync();
  }

  /// Re-establish the socket + subscription and reconcile the transcript with
  /// the server. Called on resume and whenever the socket comes back, so a
  /// turn that ran while the screen was closed reappears (with its answer).
  ///
  /// [force] lets the post-completion settle watcher run even when another
  /// reconcile is in flight — it must never be starved or the finished result
  /// may not land until the user types again.
  Future<void> _resync({bool force = false}) async {
    final chatId = _chatId;
    if (chatId == null) return;
    if (_resyncInFlight) {
      // Never drop a forced pass: the running one may have fetched the
      // transcript before the answer was written. Queue exactly one rerun.
      if (force) _resyncQueued = true;
      return;
    }
    _resyncInFlight = true;
    try {
      final state = context.read<AppState>();
      final sock = await state.ensureSocket();
      _socket = sock;
      _wireSocket(sock);
      _registerView();
      // A stalled attach must not block the transcript fetch: the history
      // comes over HTTP and does not need the socket.
      try {
        await sock.attach(chatId).timeout(const Duration(seconds: 15));
      } catch (_) {}
      if (!mounted) return;
      setState(() => _connected = sock.isConnected);

      // The gateway does not replay accumulated deltas, so pull the thread to
      // recover anything produced while this screen was not listening. This
      // also clears a stale busy pill when the turn finished in the background.
      final session = _sessionSummaryFor(state);
      if (session != null) {
        final history = await state.openSession(session);
        if (!mounted) return;
        _applyServerHistory(history);
      }
    } catch (_) {
      // Offline: keep the transcript we have and let the socket retry.
    } finally {
      _resyncInFlight = false;
      if (_resyncQueued && mounted) {
        _resyncQueued = false;
        unawaited(_resync(force: true));
      }
    }
  }

  /// The session row that describes this chat.
  ///
  /// A chat opened from "new chat" has no [widget.session], and the previous
  /// build therefore SKIPPED the history reconcile entirely for it. That is
  /// precisely why a long task started in a brand-new chat finished in the
  /// cloud but produced nothing on screen until the user sent another message:
  /// the only recovery path was the socket's running-turn replay, and if the
  /// turn had already ended by the time we re-attached, there was no replay
  /// and no history fetch. Resolving the row from the session list closes that
  /// hole — the answer is pulled from the server regardless of how the chat
  /// was created.
  SessionSummary? _sessionSummaryFor(AppState state) {
    final opened = widget.session;
    final chatId = _chatId;
    if (chatId == null) return opened;
    if (opened != null && opened.chatId == chatId) return opened;
    for (final s in state.sessions) {
      if (s.chatId == chatId) return s;
    }
    // The session list has not caught up yet (a brand-new chat, or a refresh
    // still in flight). The gateway keys every websocket session by its chat
    // id, so the canonical key is derivable — synthesising it keeps the
    // reconcile path alive instead of silently skipping the fetch, which is
    // how a finished background task previously stayed invisible.
    return SessionSummary(
      key: 'websocket:$chatId',
      chatId: chatId,
      title: opened?.title ?? '',
      preview: opened?.preview ?? '',
      updatedAt: opened?.updatedAt,
    );
  }

  Future<void> _boot() async {
    final state = context.read<AppState>();
    if (widget.session != null) {
      final session = widget.session!;
      _chatId = session.chatId;
      state.rememberChat(session.chatId);
      // Mark this chat as the one that is open, so a task still running when
      // the app is killed resumes streaming on the next launch.
      state.rememberOpenChat(session.chatId, sessionKey: session.key);
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
      // A turn may have finished while the app was closed, and the gateway can
      // take a minute or more to expose its transcript. Keep pulling until the
      // answer is really there.
      _startHydration(restart: true);
      await _attachAndWatch();
    } else {
      try {
        final sock = await state.ensureSocket();
        _socket = sock;
        _wireSocket(sock);
        final id = await sock.newChat();
        _chatId = id;
        state.rememberChat(id);
        state.rememberOpenChat(id);
        // Subscribe before anything can be sent: the gateway only streams a
        // chat's turn to sockets attached to it.
        unawaited(sock.attach(id));
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

    // Re-link the live streaming bubble before the list is swapped.
    //
    // `merged` is built from SERVER-owned ChatMessage instances, so the object
    // `_liveTurn` points at — the one every inbound `delta` appends to — is not
    // guaranteed to be in it. Without this, deltas render into a DETACHED
    // object and the transcript freezes mid-task with the busy pill still
    // spinning, which is the "long takes still get cut off" symptom. It fired
    // on every resync (turn start, every reconnect, every 15 s during a long
    // turn), so it — not the error frames the previous fix addressed — was what
    // killed the stream. See [relinkLiveTurn].
    final live = _liveTurn;
    relinkLiveTurn(
      merged: merged,
      live: live,
      activeTurnId: history.activeTurnId,
    );
    if (live != null) _liveTurn = live;

    final sockActive =
        _chatId != null && (_socket?.isTurnActive(_chatId!) ?? false);
    // A snapshot's active turn is only believed with the socket's backing once
    // a ghost run has been released for this chat. Without that fence a
    // released ghost run is re-adopted on every transcript fetch and the busy
    // state becomes permanent.
    final believeActive = _fence.shouldBelieve(
      history.activeTurnId,
      socketActive: sockActive,
    );
    setState(() {
      _messages
        ..clear()
        ..addAll(merged);
      if (believeActive) {
        _remoteRunning = true;
        _lastEventAt = DateTime.now();
      }
    });
    // The server's own transcript now carries a settled answer and no run this
    // client believes in: hydration has nothing left to recover until the next
    // turn starts.
    // "Done" means the answer for the turn we are waiting on is on the server.
    // An older answer in the transcript must not end the wait: that is exactly
    // how a finished task stayed invisible until the user sent something else.
    final pendingTurn = _pendingTurnId;
    final answered = history.messages.any((m) =>
        m.role == Role.assistant &&
        m.text.trim().isNotEmpty &&
        (pendingTurn == null || (m.turnId ?? '') == pendingTurn));
    if (!believeActive && answered) {
      _hydrateSatisfied = true;
      _pendingTurnId = null;
      _stopHydration();
    }
    _scheduleCacheWrite();
    if (believeActive) {
      // The turn is genuinely running server-side. Re-arm the live bubble and
      // make sure it is subscribed, so steps and stream keep arriving rather
      // than the transcript sitting frozen mid-task.
      _resumeLiveTurn(history.activeTurnId!);
      _startResyncWatch();
    }
  }

  /// Re-open the live bubble for a turn that is running server-side, adopting
  /// the server's turn id so the persisted copy and the streamed copy are
  /// recognised as the same turn (and never both rendered).
  ///
  /// Also guarantees the socket is subscribed to this chat: a resume must not
  /// depend on the UI having got as far as `_registerView`.
  void _resumeLiveTurn(String serverTurnId) {
    // Without a turn id there is nothing to attach to. Opening a bubble for it
    // appended an empty streaming row after an already finished answer.
    if (serverTurnId.trim().isEmpty) return;
    final chatId = _chatId;
    final existing = _liveTurn;
    if (existing != null) {
      if ((existing.turnId ?? '').isEmpty) existing.turnId = serverTurnId;
    } else {
      // Reuse the trailing assistant bubble when it is the paused/streamed
      // turn rather than the server's persisted copy of it.
      ChatMessage? target = resumableTailFor(
        messages: _messages,
        serverTurnId: serverTurnId,
      );
      if (target == null) {
        target = ChatMessage(
          id: 'live-${DateTime.now().microsecondsSinceEpoch}',
          role: Role.assistant,
          streaming: true,
          turnId: serverTurnId,
        );
        _messages.add(target);
      }
      target.streaming = true;
      target.turnId = serverTurnId;
      _liveTurn = target;
    }
    if (chatId != null && _socket != null) {
      _registerView();
      unawaited(_socket!.attach(chatId));
    }
  }

  void _wireSocket(NanobotSocket sock) {
    sock.onGoalStatus = (chatId, status) {
      if (!mounted || chatId != _chatId) return;
      _touchActivity();
      final running = status == 'running';
      setState(() => _remoteRunning = running);
      // A replayed running turn (background task, reconnect, cold start) must
      // re-open the live bubble AND keep the subscription armed, otherwise the
      // steps and stream never arrive.
      if (running) _resumeLiveTurn(_socket?.activeTurnId(chatId) ?? '');
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
      if (chatId != _chatId) return;
      _touchActivity();
      // A real turn frame is proof the server is running this chat's turn.
      // The socket only fires this for genuine turn events (never for a plain
      // terminal message), so a run that had been released as a ghost comes
      // straight back into the busy state — and any fence against it is void,
      // because the run is demonstrably alive.
      _fence.noteServedFrame();
      _ensureLiveTurn();
      if (!_remoteRunning && mounted) {
        setState(() => _remoteRunning = true);
      }
      _startResyncWatch();
      _updateWakelock();
    };
    // The server says this chat's transcript gained rows. Re-pull it: this is
    // the same trigger the WebUI uses for its canonical history refresh, and it
    // is what makes a task that finished elsewhere appear here without the
    // user having to type anything.
    sock.onThreadChanged = (chatId) {
      if (!mounted || chatId != _chatId) return;
      _startHydration();
      unawaited(_resync(force: true));
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
      streaming: true,
    );
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
        _flushNow();
        if (mounted) setState(() {});
        _scrollToBottom();
      },
      onActivity: (steps) {
        _touchActivity();
        final t = _ensureLiveTurn();
        _upsertSteps(t, steps);
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
            streaming: false,
          );
          _messages.add(turn);
        }
        final t = turn;
        // The server's final text is authoritative — adopt it whenever it is
        // non-empty. The old `text.length >= t.text.length` guard silently
        // threw away a correct-but-shorter answer, which is exactly how
        // results stopped appearing.
        t.absorbFinalText(text);
        if (media.isNotEmpty) {
          t.media = [...t.media, ...media.where((m) => !t.media.contains(m))];
        }
        _flushNow();
        if (mounted) setState(() {});
      },
      onTurnEnd: (summary) {
        _touchActivity();
        // A ghost release: the socket gave up on a running claim nothing ever
        // corroborated. Fence it so the transcript reconcile that follows
        // (which still reports that stale active turn) cannot re-open it.
        if (summary.ghost) {
          // Nothing real ended here: the next transcript fetch (which still
          // reports that stale active turn) must fence it, not adopt it.
          _fence.noteGhostRun();
        }
        // Terminal for the turn: ALWAYS terminate the busy state, even when
        // no live bubble exists (e.g. the turn ran while the screen was
        // closed). Skipping this is what let the green indicator keep
        // rolling after the task already completed.
        final terminalTurnId = summary.turnId;
        if (terminalTurnId != null && terminalTurnId.isNotEmpty) {
          // Stamp the canonical identity on the user row and the live bubble,
          // so the persisted transcript dedupes against server history on the
          // next open instead of appending the answer again.
          for (var i = _messages.length - 1; i >= 0; i--) {
            final m = _messages[i];
            if (m.role != Role.user) continue;
            if ((m.turnId ?? '').isEmpty) m.turnId = terminalTurnId;
            break;
          }
        }
        final t = _liveTurn;
        if (t != null) {
          t.streaming = false;
          t.reasoningStreaming = false;
          t.dropEmptyTrailingSegment();
          if ((t.turnId ?? '').isEmpty &&
              terminalTurnId != null &&
              terminalTurnId.isNotEmpty) {
            t.turnId = terminalTurnId;
          }
          t.usage = summary.usage ?? t.usage;
          t.latencyMs = summary.latencyMs ?? t.latencyMs;
          if (summary.media.isNotEmpty) {
            t.media = {...t.media, ...summary.media}.toList();
          }
          // The turn is finished: every step that never reported an end is
          // settled, so no row is left spinning forever.
          for (final s in t.activity) {
            if (!s.isDone) s.status = 'done';
          }
          _liveTurn = null;
        } else {
          // No live bubble (the turn ran while the screen was closed): settle
          // any trailing assistant row so its timeline stops animating.
          final last = _messages.isNotEmpty ? _messages.last : null;
          if (last != null && last.role == Role.assistant) {
            last.streaming = false;
            for (final s in last.activity) {
              if (!s.isDone) s.status = 'done';
            }
          }
        }
        _stopResyncWatch();
        if (mounted) {
          setState(() {
            _sending = false;
            _remoteRunning = false;
            _lastEventAt = DateTime.now();
          });
          _updateWakelock();
          _scrollToBottom();
          _scheduleCacheWrite(immediate: true);
          // Guarantee the completed result actually lands: pull the
          // authoritative transcript a few times over the next half minute so
          // a terminal event missed during a reconnect / background gap still
          // results in the answer being on screen — without the user having to
          // ask again.
          _armSettleWatch();
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
        if (mounted) {
          setState(() {
            _sending = false;
            _remoteRunning = false;
          });
          _updateWakelock();
          _scheduleCacheWrite();
        }
      },
      onUserMessage: (text, turnId) {
        // Projected user echo / replay after reconnect — dedupe by turnId
        // when known, else by identical user text in this view.
        //
        // A replayed echo may arrive while an optimistic bubble for the same
        // text is still the LAST user row (cold start, or a resend after the
        // app was killed). Matching the trailing row as well means the user
        // never sees their own message twice.
        final trimmed = text.trim();
        var dup = _messages.any(
          (m) =>
              m.role == Role.user &&
              ((turnId != null && m.turnId == turnId) ||
                  (turnId == null && m.text == text)),
        );
        if (!dup && trimmed.isNotEmpty) {
          // Adopt the server turn id onto a matching local row instead of
          // adding a second bubble.
          for (var i = _messages.length - 1; i >= 0; i--) {
            final m = _messages[i];
            if (m.role != Role.user) continue;
            if (m.text.trim() != trimmed) break;
            if (turnId != null && (m.turnId ?? '').isEmpty) m.turnId = turnId;
            dup = true;
            break;
          }
        }
        if (!dup && trimmed.isNotEmpty) {
          setState(
            () => _messages.add(
              ChatMessage(
                id: 'u-echo-${DateTime.now().microsecondsSinceEpoch}',
                role: Role.user,
                text: text,
                turnId: turnId,
              ),
            ),
          );
          _scrollToBottom();
        }
        if (turnId != null && turnId.isNotEmpty) {
          final live = _liveTurn;
          if (live != null && (live.turnId ?? '').isEmpty) live.turnId = turnId;
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

  /// Merge one live activity batch into the turn's timeline.
  ///
  /// Matching by `id` alone was the duplication bug: the live socket sends the
  /// real tool `call_id`, the persisted transcript replays a generated
  /// `trace-N` id and the on-disk cache carries whatever was live when it was
  /// written. The same tool call therefore arrived three times under three ids
  /// and rendered as three rows. [mergeActivitySteps] matches on tool identity
  /// plus argument summary, so a replay folds into the row already on screen.
  void _upsertSteps(ChatMessage msg, List<ActivityStep> incoming) {
    if (incoming.isEmpty) return;
    final merged = mergeActivitySteps(msg.activity, incoming);
    msg.activity
      ..clear()
      ..addAll(merged);
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
  ///
  /// The platform call is wrapped: wakelock plugins surface a
  /// MissingPluginException on some OEM builds, and an unguarded throw here
  /// ran on every turn tick — one of the paths that could take the app down
  /// mid-task.
  void _updateWakelock() {
    try {
      if (_busy) {
        WakelockPlus.enable();
        _startResyncWatch();
      } else {
        WakelockPlus.disable();
        _stopResyncWatch();
        _scheduleCacheWrite();
      }
    } catch (_) {
      // Display timeout is a nicety, never worth failing a task over.
    }
  }

  /// Coalesces high-frequency streaming deltas into UI rebuilds.
  ///
  /// One frame (16 ms) — the same cadence the web client uses via
  /// `requestAnimationFrame` — so text paints as it arrives. The previous
  /// 80 ms timer was 5x slower than the reference client and stacked on top of
  /// the socket/network hop, which is what read as "streaming is slow, it
  /// delays". Coalescing still happens (deltas are batched per frame rather
  /// than per chunk), so this does not regress the smooth-render work.
  static const Duration _streamFlushInterval = Duration(milliseconds: 16);

  void _scheduleFlush() {
    _flushTimer ??= Timer(_streamFlushInterval, () {
      _flushTimer = null;
      if (mounted) setState(() {});
      // Persisting on every frame would thrash the disk while streaming; the
      // transcript is cached on its own longer debounce instead.
      _scheduleCacheWrite();
    });
  }

  /// Flush any pending stream paint immediately.
  ///
  /// Terminal events (turn end, stream end, final message) must not wait out
  /// the frame timer, otherwise the finished answer visibly lags behind the
  /// server by up to one flush interval.
  void _flushNow() {
    if (_flushTimer == null) return;
    _flushTimer!.cancel();
    _flushTimer = null;
    if (mounted) setState(() {});
    _scheduleCacheWrite();
  }

  // ---- Turn liveness, resync & local persistence -------------------------

  void _touchActivity() {
    _lastEventAt = DateTime.now();
  }

  /// Backoff for the canonical-transcript hydration retry, in seconds.
  ///
  /// The gateway does not write a turn's transcript rows the instant the turn
  /// ends (measured against production: the HTTP thread snapshot was still
  /// empty 90-140 s after `turn_end`). A single fetch on reopen therefore races
  /// the server's write and loses — which is exactly why a task that had
  /// finished looked like it produced no result at all.
  static const List<int> _hydrateBackoff = <int>[
    3, 5, 8, 12, 18, 25, 30, 30, 30, 30, 30, 30
  ];

  /// Keep pulling the authoritative transcript until it actually contains the
  /// answer, instead of trusting one fetch that raced the server's write.
  ///
  /// Mirrors the WebUI, which re-fetches its canonical history on every
  /// non-metadata `session_updated`, on every return to the foreground, and
  /// after every turn end — never just once.
  void _startHydration({bool restart = false}) {
    if (_chatId == null) return;
    if (restart) {
      _hydrateTimer?.cancel();
      _hydrateTimer = null;
      _hydrateAttempt = 0;
      _hydrateSatisfied = false;
    }
    if (_hydrateSatisfied) return;
    _scheduleHydrateTick();
  }

  void _scheduleHydrateTick() {
    if (!mounted || _hydrateTimer != null || _hydrateSatisfied) return;
    if (_hydrateAttempt >= _hydrateBackoff.length) return;
    final wait = Duration(seconds: _hydrateBackoff[_hydrateAttempt]);
    _hydrateTimer = Timer(wait, () async {
      _hydrateTimer = null;
      _hydrateAttempt++;
      if (!mounted || _chatId == null || _hydrateSatisfied) return;
      await _resync(force: true);
      if (mounted && !_hydrateSatisfied) _scheduleHydrateTick();
    });
  }

  void _stopHydration() {
    _hydrateTimer?.cancel();
    _hydrateTimer = null;
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
      final stale =
          DateTime.now().difference(_lastEventAt) > const Duration(seconds: 90);
      if (offline || stale) {
        await _resync();
      }
    });
  }

  void _stopResyncWatch() {
    _resyncTimer?.cancel();
    _resyncTimer = null;
    _settleWatch?.cancel();
    _settleWatch = null;
  }

  /// After a terminal event, keep reconciling for a short window.
  ///
  /// A long task frequently ends while the app is backgrounded or the socket
  /// is mid-reconnect. The terminal frame is then missed, so the answer the
  /// server already persisted is never pulled into view — the reported
  /// symptom being "the task ran in the cloud but only showed up after I
  /// asked again". This watcher re-fetches history a few times after the
  /// perceived end and also recovers the case where the end was never
  /// perceived at all, guaranteeing the result lands without user input.
  void _armSettleWatch() {
    _settleWatch?.cancel();
    // The short window is for a terminal event we actually saw: the answer is
    // usually in the transcript within a few seconds then. The long hydration
    // retry covers the cases where the write lands much later. Restart it so
    // an exhausted or satisfied retry from an earlier turn cannot block this one.
    _startHydration(restart: true);
    var ticks = 0;
    _settleWatch = Timer.periodic(const Duration(seconds: 6), (t) {
      ticks++;
      if (!mounted || _busy || ticks > 5) {
        t.cancel();
        _settleWatch = null;
        return;
      }
      unawaited(_resync());
    });
  }

  /// Write the transcript to disk (debounced) so reopening the app always
  /// shows what was produced, even for a turn that never completed in-view.
  ///
  /// [immediate] bypasses the debounce — used at turn end so a completed
  /// result is durable before the process can be killed.
  void _scheduleCacheWrite({bool immediate = false}) {
    final chatId = _chatId;
    if (chatId == null) return;
    if (immediate) {
      _cacheTimer?.cancel();
      _cacheTimer = null;
      final state = context.read<AppState>();
      unawaited(state.cacheThread(chatId, List<ChatMessage>.from(_messages)));
      return;
    }
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
    _stopResyncWatch();
    setState(() {
      _sending = false;
      _remoteRunning = false;
      _lastEventAt = DateTime.now();
    });
    _updateWakelock();
    if (hadLive) _scrollToBottom();
    if (reason != null) _toast(reason);
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
    // One id names this turn end to end: the question row, the answer bubble
    // and the gateway run. The SAME id goes on the wire below; a second id
    // here made the persisted answer never match its bubble, so it was shown
    // twice after a reopen.
    final turnId = newTurnId();

    setState(() {
      _messages.add(
        ChatMessage(
          id: 'u-${DateTime.now().microsecondsSinceEpoch}',
          role: Role.user,
          text: text,
          turnId: turnId,
          media:
              readyMedia
                  .map((a) => a.url ?? (a.localPath ?? ''))
                  .where((s) => s.isNotEmpty)
                  .toList(),
        ),
      );
      // A fresh answer bubble for THIS question, after its question row. The
      // previous turn's bubble is retired so late events cannot write into it.
      _liveTurn = beginLiveTurn(
        messages: _messages,
        stale: _liveTurn,
        turnId: turnId,
      );
      _pendingTurnId = turnId;
      _hydrateSatisfied = false;
      _pending.clear();
      _sending = true;
      _lastEventAt = DateTime.now();
    });
    _updateWakelock();
    _scrollToBottom(force: true);
    _scheduleCacheWrite();
    _startHydration(restart: true);

    try {
      _socket ??= await state.ensureSocket();
      _wireSocket(_socket!);
      if (_chatId == null) {
        _chatId = await _socket!.newChat();
        state.rememberChat(_chatId!);
        // Subscribe to the chat we just created, so this device receives the
        // turn it is about to start (and any turn resuming from before).
        unawaited(_socket!.attach(_chatId!));
      }
      _registerView();
      setState(() => _connected = _socket!.isConnected);
    } catch (e) {
      _fail('Connection error: $e');
      return;
    }

    _ensureLiveTurn();
    // Mark the turn before the frame hits the wire so an early terminal event
    // is still attributed to this turn. The client names the turn (WebUI
    // parity) so the gateway can tag its run and replays with the same id.
    _socket!.markUserTurn(_chatId!);
    _socket!.sendMessage(
      _chatId!,
      text,
      media: wireMedia.isEmpty ? null : wireMedia,
      turnId: turnId,
    );
  }

  /// Cancel the running task. WebUI parity: the run is released at once (the
  /// socket settles it locally and sends `/stop`), so the stop control never
  /// stays up waiting for a server acknowledgement.
  void _stop() {
    final chatId = _chatId;
    if (chatId == null || _socket == null || !_busy) return;
    _touchActivity();
    _socket!.stopTask(chatId);
    _forceClearRunning();
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
      final payload = await state.api.fetchFilePreview(
        state.apiToken!,
        key,
        path: path,
        supabaseToken: state.accessToken,
      );
      final content = payload['content'];
      if (content is! String || content.isEmpty) {
        throw StateError('file is empty or not text-previewable');
      }
      final name = path.split('/').last;
      final dir = await getTemporaryDirectory();
      final file = File(
        '${dir.path}/${DateTime.now().millisecondsSinceEpoch}-$name',
      );
      await file.writeAsString(content, flush: true);
      final result = await OpenFilex.open(file.path);
      if (result.type != ResultType.done) {
        _toast('Saved to ${file.path} (no viewer for this type).');
      }
    } on ApiException catch (e) {
      if (e.status == 415) {
        _toast(
          '"${path.split('/').last}" is binary — ask the agent to send it '
          'as an attachment to download it.',
        );
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

  /// Overflow menu: the actions that used to crowd the app bar.
  void _showChatMenu(BuildContext context) {
    showModalBottomSheet<void>(
      context: context,
      backgroundColor: Palette.bg2,
      showDragHandle: true,
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(20)),
      ),
      builder:
          (sheet) => SafeArea(
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                ListTile(
                  leading: const Icon(Icons.settings_outlined, size: 20),
                  title: const Text('Settings'),
                  onTap: () {
                    Navigator.pop(sheet);
                    Navigator.of(context).pushNamed('/settings');
                  },
                ),
                ListTile(
                  leading: const Icon(Icons.refresh_rounded, size: 20),
                  title: const Text('Reload conversation'),
                  onTap: () {
                    Navigator.pop(sheet);
                    unawaited(_resync(force: true));
                  },
                ),
                ListTile(
                  leading:  Icon(
                    Icons.copy_all_rounded,
                    size: 20,
                    color: Palette.textSecondary,
                  ),
                  title: const Text('Copy conversation'),
                  onTap: () {
                    Navigator.pop(sheet);
                    final all = _messages
                        .where((m) => m.text.trim().isNotEmpty)
                        .map((m) => m.text.trim())
                        .join('\n\n');
                    if (all.isEmpty) {
                      _toast('Nothing to copy yet.');
                      return;
                    }
                    Clipboard.setData(ClipboardData(text: all));
                    _toast('Conversation copied.');
                  },
                ),
              ],
            ),
          ),
    );
  }

  @override
  void dispose() {
    _flushTimer?.cancel();
    _resyncTimer?.cancel();
    _settleWatch?.cancel();
    _hydrateTimer?.cancel();
    _recordTick?.cancel();
    // Never leave the mic open: a recorder outliving the screen keeps the
    // Android mic indicator on and blocks other apps.
    if (_recording) {
      unawaited(_recorder.stop().then(_deleteQuietly).catchError((_) => ''));
    }
    unawaited(_recorder.dispose());
    _cacheTimer?.cancel();
    // Detach the UI listener and persist the transcript, which keeps the
    // result visible on reopen. The chat's socket WINDOW is deliberately kept
    // alive: leaving this screen must not unsubscribe it, otherwise a turn
    // that is still running stops streaming and its steps/answer are lost.
    final chatId = _chatId;
    if (chatId != null) {
      unawaited(
        context.read<AppState>().cacheThread(
          chatId,
          List<ChatMessage>.from(_messages),
        ),
      );
    }
    // Keep the screen awake if a turn is still running for this chat — the
    // socket keeps streaming into it even though the UI is gone.
    if (!_busy) WakelockPlus.disable();
    WidgetsBinding.instance.removeObserver(this);
    if (chatId != null) _socket?.unlisten(chatId);
    _input.dispose();
    _scroll.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final state = context.watch<AppState>();
    return Scaffold(
      backgroundColor: Palette.bg0,
      appBar: AppBar(
        backgroundColor: Palette.bg1,
        elevation: 0,
        // Compact workspace bar: back affordance on the left, a two-line
        // title (app + live model) in the middle, status on the right. Nothing
        // shifts while a turn streams because the status slot is fixed-size.
        leading:
            Navigator.of(context).canPop()
                ? IconButton(
                  icon: const Icon(Icons.arrow_back_rounded, size: 21),
                  tooltip: 'Back',
                  onPressed: () => Navigator.of(context).maybePop(),
                )
                : null,
        titleSpacing: 0,
        title: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const BrandWordmark(fontSize: 15),
            Text(
              state.modelName?.isNotEmpty == true
                  ? state.modelName!
                  : 'Ready',
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style:  TextStyle(
                fontSize: 11,
                height: 1.3,
                color: Palette.textTertiary,
                fontWeight: FontWeight.w500,
              ),
            ),
          ],
        ),
        centerTitle: false,
        actions: [
          if (!_connected)
             Padding(
              padding: EdgeInsets.only(right: 2),
              child: Tooltip(
                message: 'Reconnecting…',
                child: Icon(
                  Icons.cloud_off_rounded,
                  size: 18,
                  color: Palette.warning,
                ),
              ),
            ),
          const ThemeToggleButton(),
          IconButton(
            icon: const Icon(Icons.more_horiz_rounded, size: 21),
            tooltip: 'More',
            onPressed: () => _showChatMenu(context),
          ),
        ],
      ),
      body: Column(
        children: [
          if (!_connected)
            Container(
              width: double.infinity,
              color: Palette.bg3,
              padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 7),
              child:  Row(
                children: [
                  Icon(
                    Icons.cloud_off_rounded,
                    size: 15,
                    color: Palette.warning,
                  ),
                  SizedBox(width: 8),
                  Expanded(
                    child: Text(
                      'Reconnecting… your task keeps running on the server.',
                      style: TextStyle(fontSize: 12, color: Palette.warning),
                    ),
                  ),
                ],
              ),
            ),
          Expanded(
            child:
                _loadingHistory
                    ?  Center(
                      child: CircularProgressIndicator(
                        strokeWidth: 2.4,
                        color: Palette.accent,
                      ),
                    )
                    : _messages.isEmpty
                    ? _EmptyChat(greetingName: state.greetingName)
                    : ListView.builder(
                      controller: _scroll,
                      // Cache a small window only: with a 400-message bounded
                      // transcript the previous generous cacheExtent kept dead
                      // render objects (and their markdown trees) alive, which
                      // is what made long chats feel heavy while scrolling.
                      cacheExtent: 480,
                      padding: const EdgeInsets.fromLTRB(14, 10, 14, 10),
                      itemCount: _messages.length,
                      itemBuilder:
                          (_, i) => _Bubble(
                            // Stable identity per row: without a key a re-sorted
                            // or merged transcript rebuilt every bubble from
                            // scratch on each streaming flush.
                            key: ValueKey(_messages[i].id),
                            message: _messages[i],
                            onOpenArtifact: (p) => _openArtifact(p),
                          ),
                    ),
          ),
          _Composer(
            controller: _input,
            busy: _busy,
            pending: _pending,
            onPick: _pickFiles,
            onRemove: (a) => setState(() => _pending.remove(a)),
            onSend: _send,
            onStop: _stop,
            onVoice: _toggleVoiceNote,
            recording: _recording,
            transcribing: _transcribing,
            recordSeconds: _recordSeconds,
            hint:
                _busy
                    ? 'Model is responding…'
                    : (_messages.isEmpty
                        ? 'Ask anything...'
                        : 'Type your message…'),
          ),
        ],
      ),
    );
  }
}

/// Shown on a brand-new, still-empty conversation: a quiet prompt that tells
/// the user the agent is ready without shouting.
class _EmptyChat extends StatelessWidget {
  const _EmptyChat({required this.greetingName});
  final String greetingName;

  @override
  Widget build(BuildContext context) {
    return Center(
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 32),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            const BrandMark(size: 66),
            const SizedBox(height: 20),
            Text(
              'Hi $greetingName',
              style:  TextStyle(
                fontSize: 21,
                fontWeight: FontWeight.w800,
                color: Palette.textPrimary,
              ),
            ),
            const SizedBox(height: 8),
             Text(
              'Ask a question, attach a file, or describe a task.\n'
              'I can research, write, analyse data and build things.',
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
    );
  }
}

class _Bubble extends StatelessWidget {
  const _Bubble({
    super.key,
    required this.message,
    this.onOpenArtifact,
  });
  final ChatMessage message;
  final void Function(String path)? onOpenArtifact;

  @override
  Widget build(BuildContext context) {
    return message.role == Role.user
        ? _userTurn(context)
        : _assistantTurn(context);
  }

  void _copy(BuildContext context) {
    if (message.text.trim().isEmpty) return;
    Clipboard.setData(ClipboardData(text: message.text));
    ScaffoldMessenger.of(context).showSnackBar(
      const SnackBar(
        content: Text('Copied to clipboard'),
        duration: Duration(seconds: 1),
      ),
    );
  }

  /// Web: the user turn is a right-aligned pill capped at 85% width (36rem).
  Widget _userTurn(BuildContext context) {
    final width = MediaQuery.of(context).size.width;
    final maxWidth = width * 0.85 < 576 ? width * 0.85 : 576.0;
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 6),
      child: Align(
        alignment: Alignment.centerRight,
        child: ConstrainedBox(
          constraints: BoxConstraints(maxWidth: maxWidth),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.end,
            mainAxisSize: MainAxisSize.min,
            children: [
              if (message.text.isNotEmpty)
                GestureDetector(
                  onLongPress: () => _copy(context),
                  child: Container(
                    padding: const EdgeInsets.symmetric(
                      horizontal: 16,
                      vertical: 9,
                    ),
                    decoration: BoxDecoration(
                      gradient: Palette.userBubbleGradient,
                      borderRadius: BorderRadius.circular(18),
                    ),
                    child: SelectableText(
                      message.text,
                      style: TextStyle(
                        color: Palette.userText,
                        fontSize: 16,
                        height: 1.5,
                      ),
                    ),
                  ),
                ),
              if (message.media.isNotEmpty)
                Padding(
                  padding: EdgeInsets.only(
                    top: message.text.isNotEmpty ? 8 : 0,
                  ),
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
          ),
        ),
      ),
    );
  }

  /// Web: the assistant turn is flat, full-width prose with no avatar or card.
  Widget _assistantTurn(BuildContext context) {
    final hasActivity =
        message.activity.isNotEmpty || message.reasoning.trim().isNotEmpty;
    final body = Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      mainAxisSize: MainAxisSize.min,
      children: [
        if (hasActivity) _ActivityCluster(message: message),
        if (message.artifactPaths.isNotEmpty)
          Padding(
            padding: const EdgeInsets.only(bottom: 8),
            child: _FileChips(
              paths: message.artifactPaths,
              onOpen: onOpenArtifact,
            ),
          ),
        for (var i = 0; i < message.segments.length; i++)
          Padding(
            padding: EdgeInsets.only(top: i == 0 ? 0 : 8),
            child: MarkdownBody(
              data: message.segments[i],
              selectable: true,
              styleSheet: AppTheme.markdown(context),
              extensionSet: kLatexExtensionSet,
              builders: kLatexBuilders,
            ),
          ),
        if (message.streaming && message.isEmpty && !hasActivity)
          Padding(
            padding: const EdgeInsets.only(top: 2),
            child: Text(
              'Thinking…',
              style: TextStyle(fontSize: 13, color: Palette.textTertiary),
            ),
          ),
        if (message.viewableMedia.isNotEmpty)
          Padding(
            padding: const EdgeInsets.only(top: 8),
            child: Wrap(
              spacing: 8,
              runSpacing: 8,
              children: [
                for (final u in message.viewableMedia) _MediaLink(url: u),
              ],
            ),
          ),
        if (!message.streaming && message.hasError)
          Padding(
            padding: const EdgeInsets.only(top: 6),
            child: Icon(Icons.error_outline, size: 16, color: Palette.danger),
          ),
        // Web: a quiet copy action under a finished reply (MessageCopyButton).
        if (!message.streaming && message.text.trim().isNotEmpty)
          Padding(
            padding: const EdgeInsets.only(top: 4),
            child: _CopyReplyButton(text: message.text),
          ),
      ],
    );
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 6),
      child: GestureDetector(
        // Long-press copies the answer text, handy on a phone.
        onLongPress: () => _copy(context),
        child: SizedBox(width: double.infinity, child: body),
      ),
    );
  }

  static String _basename(String path) {
    final clean = path.split('?').first;
    final segs = clean.split('/');
    return segs.last.isEmpty ? clean : segs.last;
  }
}

/// Web parity (MessageCopyButton): a 32px ghost icon that swaps to a check
/// for two seconds after the reply is copied.
class _CopyReplyButton extends StatefulWidget {
  const _CopyReplyButton({required this.text});
  final String text;
  @override
  State<_CopyReplyButton> createState() => _CopyReplyButtonState();
}

class _CopyReplyButtonState extends State<_CopyReplyButton> {
  bool _copied = false;
  Timer? _reset;

  @override
  void dispose() {
    _reset?.cancel();
    super.dispose();
  }

  Future<void> _copy() async {
    await Clipboard.setData(ClipboardData(text: widget.text));
    if (!mounted) return;
    setState(() => _copied = true);
    _reset?.cancel();
    _reset = Timer(const Duration(seconds: 2), () {
      if (mounted) setState(() => _copied = false);
    });
  }

  @override
  Widget build(BuildContext context) {
    final label = _copied ? 'Copied' : 'Copy reply';
    return Tooltip(
      message: label,
      child: Semantics(
        button: true,
        label: label,
        child: InkWell(
          customBorder: const CircleBorder(),
          onTap: _copy,
          child: SizedBox(
            width: 32,
            height: 32,
            child: Icon(
              _copied ? Icons.check_rounded : Icons.copy_rounded,
              size: 16,
              color: Palette.textSecondary,
            ),
          ),
        ),
      ),
    );
  }
}

/// Web parity (AgentActivityCluster): one collapsible row above the answer
/// reading "Working for 12s" / "Worked for 12s" (or "Thinking for" /
/// "Thought for" when the turn had no tool calls). It opens while the turn
/// streams, closes about 300 ms after the turn ends, and the user can toggle
/// it at any time. Reasoning and tool steps are rows inside it.
class _ActivityCluster extends StatefulWidget {
  const _ActivityCluster({required this.message});
  final ChatMessage message;

  @override
  State<_ActivityCluster> createState() => _ActivityClusterState();
}

class _ActivityClusterState extends State<_ActivityCluster> {
  bool? _userOpen; // null = follow the live turn state
  bool _holdOpen = false; // stays open briefly after the turn ends
  bool _wasLive = false;
  Timer? _holdTimer;
  Timer? _ticker; // refreshes the live duration once a second
  final ScrollController _scroll = ScrollController();

  @override
  void initState() {
    super.initState();
    _sync();
  }

  @override
  void didUpdateWidget(covariant _ActivityCluster old) {
    super.didUpdateWidget(old);
    _sync();
  }

  void _sync() {
    final live = widget.message.streaming;
    if (live) {
      _holdTimer?.cancel();
      _holdOpen = false;
      _ticker ??= Timer.periodic(const Duration(seconds: 1), (_) {
        if (mounted) setState(() {});
      });
      // Keep the newest row in view while the turn streams.
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (mounted && _scroll.hasClients) {
          _scroll.jumpTo(_scroll.position.maxScrollExtent);
        }
      });
    } else {
      _ticker?.cancel();
      _ticker = null;
      if (_wasLive && _userOpen == null) {
        _holdOpen = true;
        _holdTimer?.cancel();
        _holdTimer = Timer(const Duration(milliseconds: 300), () {
          if (mounted) setState(() => _holdOpen = false);
        });
      }
    }
    _wasLive = live;
  }

  @override
  void dispose() {
    _holdTimer?.cancel();
    _ticker?.cancel();
    _scroll.dispose();
    super.dispose();
  }

  bool get _expanded =>
      _userOpen ?? (widget.message.streaming || _holdOpen);

  int get _durationMs {
    final m = widget.message;
    if (m.streaming) {
      return DateTime.now().difference(m.createdAt).inMilliseconds;
    }
    return m.latencyMs ?? 0;
  }

  static String _formatDuration(int ms) {
    final seconds = ms > 0 && ms < 1000 ? 1 : (ms / 1000).round();
    if (seconds < 60) return '${seconds}s';
    final minutes = seconds ~/ 60;
    final rest = seconds % 60;
    return rest > 0 ? '${minutes}m ${rest}s' : '${minutes}m';
  }

  String get _label {
    final m = widget.message;
    final hasTools = m.activity.isNotEmpty;
    final ms = _durationMs;
    final dur = _formatDuration(ms);
    if (m.streaming) {
      return hasTools ? 'Working for $dur' : 'Thinking for $dur';
    }
    if (hasTools) return ms <= 0 ? 'Worked' : 'Worked for $dur';
    return ms <= 0 ? 'Thought' : 'Thought for $dur';
  }

  @override
  Widget build(BuildContext context) {
    final m = widget.message;
    final expanded = _expanded;
    final hasDetails =
        m.activity.isNotEmpty || m.reasoning.trim().isNotEmpty;
    final hasBodyBelow = m.segments.any((s) => s.trim().isNotEmpty);

    final header = Padding(
      padding: const EdgeInsets.symmetric(vertical: 3),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Flexible(
            child: Text(
              _label,
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: TextStyle(
                fontSize: 13,
                height: 1.4,
                fontWeight: FontWeight.w500,
                color: Palette.textTertiary.withValues(alpha: 0.8),
              ),
            ),
          ),
          if (hasDetails) ...[
            const SizedBox(width: 4),
            AnimatedRotation(
              turns: expanded ? 0.5 : 0,
              duration: const Duration(milliseconds: 220),
              child: Icon(
                Icons.keyboard_arrow_down_rounded,
                size: 15,
                color: Palette.textTertiary,
              ),
            ),
          ],
        ],
      ),
    );

    return Padding(
      padding: EdgeInsets.only(bottom: hasBodyBelow ? 8 : 0),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        mainAxisSize: MainAxisSize.min,
        children: [
          if (hasDetails)
            InkWell(
              borderRadius: BorderRadius.circular(6),
              onTap: () => setState(() => _userOpen = !expanded),
              child: header,
            )
          else
            header,
          if (hasDetails)
            AnimatedSize(
              duration: const Duration(milliseconds: 220),
              curve: Curves.easeOut,
              alignment: Alignment.topCenter,
              child: expanded ? _body() : const SizedBox.shrink(),
            ),
        ],
      ),
    );
  }

  Widget _body() {
    final m = widget.message;
    final reasoning = m.reasoning.trim();
    return Container(
      margin: const EdgeInsets.only(top: 4),
      constraints: const BoxConstraints(maxHeight: 180),
      child: SingleChildScrollView(
        controller: _scroll,
        padding: const EdgeInsets.only(right: 4),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          mainAxisSize: MainAxisSize.min,
          children: [
            if (reasoning.isNotEmpty)
              _ActivityRow(
                marker: _RowMarker(
                  status: m.reasoningStreaming && m.streaming
                      ? 'running'
                      : 'done',
                ),
                label: _reasoningPreview(
                  reasoning,
                  streaming: m.reasoningStreaming && m.streaming,
                ),
                italic: true,
              ),
            for (final step in m.activity)
              _ActivityRow(
                marker: _RowMarker(status: step.status),
                label:
                    step.detail.isNotEmpty
                        ? '${step.name} · ${step.detail}'
                        : step.name,
                error: step.status == 'error',
              ),
          ],
        ),
      ),
    );
  }

  /// Web compactReasoningPreview: strip markdown and collapse whitespace.
  static String _reasoningPreview(String text, {required bool streaming}) {
    final cleaned = text
        .replaceAllMapped(
          RegExp(r'\[([^\]]+)\]\([^)]+\)'),
          (m) => m.group(1)!,
        )
        .replaceAll(RegExp(r'[*_#`~]+'), '')
        .replaceAll(RegExp(r'\s+'), ' ')
        .trim();
    if (cleaned.isNotEmpty) return cleaned;
    return streaming ? 'Thinking…' : 'Thinking';
  }
}

/// One line inside the activity cluster: a fixed marker column, then a
/// single truncated label, matching the web ActivityStep layout.
class _ActivityRow extends StatelessWidget {
  const _ActivityRow({
    required this.marker,
    required this.label,
    this.italic = false,
    this.error = false,
  });
  final Widget marker;
  final String label;
  final bool italic;
  final bool error;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 2),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          SizedBox(
            width: 18,
            height: 20,
            child: Center(child: marker),
          ),
          const SizedBox(width: 6),
          Expanded(
            child: Text(
              label,
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: TextStyle(
                fontSize: 13,
                height: 20 / 13,
                fontWeight: FontWeight.w500,
                fontStyle: italic ? FontStyle.italic : FontStyle.normal,
                color:
                    error
                        ? Palette.danger.withValues(alpha: 0.85)
                        : Palette.textSecondary.withValues(alpha: 0.85),
              ),
            ),
          ),
        ],
      ),
    );
  }
}

/// Status marker for a cluster row. running spins; done is a green check;
/// refused is a neutral amber shield (a guard said no, not a failure); error
/// is a red cross.
class _RowMarker extends StatelessWidget {
  const _RowMarker({required this.status});
  final String status;

  @override
  Widget build(BuildContext context) {
    if (status == 'running') {
      return SizedBox(
        width: 12,
        height: 12,
        child: CircularProgressIndicator(
          strokeWidth: 1.6,
          color: Palette.textTertiary,
        ),
      );
    }
    final Color color;
    final IconData icon;
    switch (status) {
      case 'error':
        color = Palette.danger;
        icon = Icons.close_rounded;
      case 'refused':
        color = Palette.warning;
        icon = Icons.shield_outlined;
      default:
        color = Palette.success;
        icon = Icons.check_rounded;
    }
    return Container(
      width: 14,
      height: 14,
      alignment: Alignment.center,
      decoration: BoxDecoration(
        shape: BoxShape.circle,
        border: Border.all(color: color.withValues(alpha: 0.4)),
      ),
      child: Icon(icon, size: 10, color: color),
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
      borderRadius: BorderRadius.circular(8),
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
        decoration: BoxDecoration(
          color: Palette.scrim(0.25),
          borderRadius: BorderRadius.circular(8),
          border: Border.all(color: Palette.borderSoft),
        ),
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            const Icon(Icons.attach_file, size: 14, color: Colors.white70),
            const SizedBox(width: 6),
            ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 160),
              child: Text(
                label,
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: const TextStyle(color: Colors.white70, fontSize: 12),
              ),
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
            errorBuilder:
                (_, __, ___) =>
                    _AttachmentChip(label: _basename(url), url: url),
            loadingBuilder:
                (_, child, prog) =>
                    prog == null
                        ? child
                        :  SizedBox(
                          width: 120,
                          height: 120,
                          child: Center(
                            child: CircularProgressIndicator(
                              strokeWidth: 2,
                              color: Palette.accent,
                            ),
                          ),
                        ),
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

class _Composer extends StatefulWidget {
  const _Composer({
    required this.controller,
    required this.busy,
    required this.pending,
    required this.onPick,
    required this.onRemove,
    required this.onSend,
    required this.onStop,
    required this.onVoice,
    required this.recording,
    required this.transcribing,
    required this.recordSeconds,
    required this.hint,
  });
  final TextEditingController controller;
  final bool busy;
  /// Placeholder text, chosen like the web composer.
  final String hint;
  final List<PendingAttachment> pending;
  final VoidCallback onPick;
  final void Function(PendingAttachment) onRemove;
  final VoidCallback onSend;
  final VoidCallback onStop;

  /// Start/stop a voice note. The screen owns recording so the mic state
  /// survives this widget rebuilding on every streamed delta.
  final VoidCallback onVoice;

  /// Live voice-note state, rendered as a strip above the input while the
  /// user is talking and while the clip is being transcribed.
  final bool recording;
  final bool transcribing;
  final int recordSeconds;

  @override
  State<_Composer> createState() => _ComposerState();
}

class _ComposerState extends State<_Composer> {
  /// Manus-style tools tray: the "+" reveals a row of quick actions above the
  /// input instead of dumping the user straight into a system file picker.
  bool _toolsOpen = false;

  /// Tracks focus so the pill fills to the web's focused tint (bg-muted/50).
  final FocusNode _focus = FocusNode();

  @override
  void initState() {
    super.initState();
    _focus.addListener(() {
      if (mounted) setState(() {});
    });
  }

  @override
  void dispose() {
    _focus.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return SafeArea(
      top: false,
      child: Container(
        padding: const EdgeInsets.fromLTRB(12, 8, 12, 10),
        decoration:  BoxDecoration(
          color: Palette.bg0,
          border: Border(top: BorderSide(color: Palette.borderSoft)),
        ),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            if (widget.pending.isNotEmpty)
              Padding(
                padding: const EdgeInsets.only(bottom: 8),
                child: SizedBox(
                  height: 62,
                  child: ListView(
                    scrollDirection: Axis.horizontal,
                    children: [
                      for (final a in widget.pending)
                        Padding(
                          padding: const EdgeInsets.only(right: 8),
                          child: _PendingTile(
                            attachment: a,
                            onRemove: () => widget.onRemove(a),
                          ),
                        ),
                    ],
                  ),
                ),
              ),
            // Quick-action tray, mirroring the reference app's "+" menu.
            // Live voice-note strip: shows while the user is talking and while
            // the clip is being turned into text, so the mic never looks stuck.
            if (widget.recording || widget.transcribing)
              Padding(
                padding: const EdgeInsets.only(bottom: 8),
                child: Container(
                  padding: const EdgeInsets.symmetric(
                    horizontal: 14,
                    vertical: 11,
                  ),
                  decoration: BoxDecoration(
                    color: widget.recording
                        ? Palette.danger.withValues(alpha: 0.12)
                        : Palette.bg2,
                    borderRadius: BorderRadius.circular(14),
                    border: Border.all(
                      color: widget.recording
                          ? Palette.danger.withValues(alpha: 0.45)
                          : Palette.borderSoft,
                    ),
                  ),
                  child: Row(
                    children: [
                      if (widget.recording)
                        const _PulsingDot()
                      else
                         SizedBox(
                          width: 14,
                          height: 14,
                          child: CircularProgressIndicator(
                            strokeWidth: 2,
                            color: Palette.accent,
                          ),
                        ),
                      const SizedBox(width: 10),
                      Expanded(
                        child: Text(
                          widget.recording
                              ? 'Listening… tap to stop'
                              : 'Transcribing voice note…',
                          style:  TextStyle(
                            fontSize: 13,
                            color: Palette.textPrimary,
                            fontWeight: FontWeight.w600,
                          ),
                        ),
                      ),
                      if (widget.recording)
                        Text(
                          '${(widget.recordSeconds ~/ 60).toString().padLeft(2, '0')}:'
                          '${(widget.recordSeconds % 60).toString().padLeft(2, '0')}',
                          style:  TextStyle(
                            fontSize: 13,
                            color: Palette.danger,
                            fontWeight: FontWeight.w700,
                            fontFeatures: [FontFeature.tabularFigures()],
                          ),
                        ),
                    ],
                  ),
                ),
              ),
            AnimatedSize(
              duration: const Duration(milliseconds: 160),
              curve: Curves.easeOut,
              child:
                  _toolsOpen
                      ? Padding(
                        padding: const EdgeInsets.only(bottom: 8),
                        child: Row(
                          children: [
                            _ToolChip(
                              icon: Icons.attach_file_rounded,
                              label: 'Attach file',
                              onTap: () {
                                setState(() => _toolsOpen = false);
                                widget.onPick();
                              },
                            ),
                            const SizedBox(width: 8),
                            _ToolChip(
                              icon: Icons.camera_alt_outlined,
                              label: 'Photo',
                              onTap: () {
                                setState(() => _toolsOpen = false);
                                widget.onPick();
                              },
                            ),
                            const SizedBox(width: 8),
                            _ToolChip(
                              icon: Icons.keyboard_voice_outlined,
                              label: 'Voice note',
                              onTap: () {
                                setState(() => _toolsOpen = false);
                                widget.onVoice();
                              },
                            ),
                          ],
                        ),
                      )
                      : const SizedBox.shrink(),
            ),
            // One elevated pill holds attach + input + send.
            Container(
              decoration: BoxDecoration(
                color: _focus.hasFocus
                    ? Palette.active.composerSurfaceFocused
                    : Palette.active.composerSurface,
                borderRadius: BorderRadius.circular(22),
              ),
              padding: const EdgeInsets.fromLTRB(4, 4, 6, 4),
              child: Row(
                crossAxisAlignment: CrossAxisAlignment.end,
                children: [
                  IconButton(
                    onPressed:
                        () => setState(() => _toolsOpen = !_toolsOpen),
                    icon: Icon(
                      _toolsOpen ? Icons.close_rounded : Icons.add_rounded,
                      color: Palette.textSecondary,
                      size: 22,
                    ),
                    tooltip: 'Tools',
                  ),
                  Expanded(
                    child: TextField(
                      controller: widget.controller,
                      focusNode: _focus,
                      minLines: 1,
                      maxLines: 5,
                      textInputAction: TextInputAction.newline,
                      style:  TextStyle(
                        color: Palette.textPrimary,
                        fontSize: 15,
                      ),
                      decoration: InputDecoration(
                        hintText: widget.hint,
                        hintStyle:  TextStyle(
                          color: Palette.textTertiary,
                          fontSize: 15,
                        ),
                        filled: false,
                        isDense: true,
                        contentPadding: const EdgeInsets.symmetric(
                          vertical: 12,
                        ),
                        border: InputBorder.none,
                        enabledBorder: InputBorder.none,
                        focusedBorder: InputBorder.none,
                      ),
                    ),
                  ),
                  const SizedBox(width: 6),
                  // Mic sits next to send so a voice note is always one tap
                  // away, and turns into a stop control while recording.
                  if (!widget.transcribing)
                    IconButton(
                      onPressed: widget.onVoice,
                      icon: Icon(
                        widget.recording
                            ? Icons.stop_circle_rounded
                            : Icons.mic_none_rounded,
                        color: widget.recording
                            ? Palette.danger
                            : Palette.textSecondary,
                        size: 22,
                      ),
                      tooltip: widget.recording
                          ? 'Stop and transcribe'
                          : 'Record a voice note',
                    ),
                  // Matches the web composer: a 36px circle. Send is the
                  // strong (foreground) disc; while the agent works it becomes
                  // a quiet bordered disc with a small square stop glyph.
                  SizedBox(
                    width: 36,
                    height: 36,
                    child: Material(
                      color: widget.busy ? Palette.bg2 : Palette.textPrimary,
                      shape: CircleBorder(
                        side: BorderSide(
                          color: widget.busy ? Palette.border : Palette.textPrimary,
                        ),
                      ),
                      shadowColor: const Color(0x2E0F172A),
                      elevation: widget.busy ? 1 : 2,
                      child: InkWell(
                        customBorder: const CircleBorder(),
                        onTap:
                            widget.busy
                                ? widget.onStop
                                : widget.onSend,
                        child: Center(
                          child:
                              widget.busy
                                  ? Icon(
                                    Icons.stop_rounded,
                                    color: Palette.textPrimary.withValues(alpha: 0.85),
                                    size: 14,
                                  )
                                  : Icon(
                                    Icons.arrow_upward_rounded,
                                    color: Palette.bg0,
                                    size: 16,
                                  ),
                        ),
                      ),
                    ),
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

/// A small breathing red dot used on the recording strip.
class _PulsingDot extends StatefulWidget {
  const _PulsingDot();
  @override
  State<_PulsingDot> createState() => _PulsingDotState();
}

class _PulsingDotState extends State<_PulsingDot>
    with SingleTickerProviderStateMixin {
  late final AnimationController _c = AnimationController(
    vsync: this,
    duration: const Duration(milliseconds: 800),
  )..repeat(reverse: true);

  @override
  void dispose() {
    _c.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return FadeTransition(
      opacity: Tween(begin: 0.35, end: 1.0).animate(_c),
      child: Container(
        width: 12,
        height: 12,
        decoration:  BoxDecoration(
          color: Palette.danger,
          shape: BoxShape.circle,
        ),
      ),
    );
  }
}

/// One pill in the composer's quick-action tray.
class _ToolChip extends StatelessWidget {
  const _ToolChip({
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
      borderRadius: BorderRadius.circular(12),
      child: InkWell(
        borderRadius: BorderRadius.circular(12),
        onTap: onTap,
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              Icon(icon, size: 16, color: Palette.accentSoft),
              const SizedBox(width: 6),
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
            color: Palette.bg3,
            borderRadius: BorderRadius.circular(10),
            border: Border.all(
              color: attachment.isError ? Palette.danger : Palette.border,
            ),
          ),
          clipBehavior: Clip.antiAlias,
          child:
              isImage
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
                          color: Palette.textTertiary,
                        ),
                        const SizedBox(height: 2),
                        Text(
                          _shortName(attachment.name),
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style:  TextStyle(
                            color: Palette.textTertiary,
                            fontSize: 9,
                          ),
                        ),
                      ],
                    ),
                  ),
        ),
        if (attachment.status == 'uploading')
          Positioned.fill(
            child: ColoredBox(
              color: Palette.scrim(0.5),
              child:  Center(
                child: SizedBox(
                  width: 18,
                  height: 18,
                  child: CircularProgressIndicator(
                    strokeWidth: 2,
                    color: Palette.accent,
                  ),
                ),
              ),
            ),
          ),
        if (attachment.isError)
          Positioned.fill(
            child: Tooltip(
              message: attachment.errorText ?? 'Upload failed',
              child: ColoredBox(
                color: Palette.scrim(0.55),
                child:  Center(
                  child: Icon(
                    Icons.error_outline,
                    color: Palette.danger,
                    size: 22,
                  ),
                ),
              ),
            ),
          ),
        Positioned(
          top: -6,
          right: -6,
          child: GestureDetector(
            onTap: onRemove,
            child: Container(
              decoration:  BoxDecoration(
                color: Palette.accentDeep,
                shape: BoxShape.circle,
              ),
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
                color: Palette.scrim(0.35),
                borderRadius: BorderRadius.circular(8),
                border: Border.all(color: Palette.border),
              ),
              child: Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                   Icon(
                    Icons.insert_drive_file_outlined,
                    size: 14,
                    color: Palette.accentSoft,
                  ),
                  const SizedBox(width: 5),
                  Text(
                    _fileBaseName(p),
                    style:  TextStyle(
                      color: Palette.textPrimary,
                      fontSize: 12,
                    ),
                  ),
                  const SizedBox(width: 4),
                   Icon(
                    Icons.download_rounded,
                    size: 14,
                    color: Palette.textTertiary,
                  ),
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
