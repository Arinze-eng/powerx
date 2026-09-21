/// Canonical-run fencing: which `active turn` a transcript snapshot is
/// allowed to put the UI into.
///
/// Why this exists. The gateway keeps a run's wall-clock in module-global
/// state and replays it as `goal_status: running` on every attach
/// (`_maybe_push_turn_run_wall_clock` in `nanobot/channels/websocket/
/// runtime.py`). When the socket that owned a run dies mid-task, nothing ever
/// clears that state — `send_turn_end` early-returns because there are no
/// subscribers left — so from then on the chat looks permanently "running" to
/// every new client. The same stale wall-clock also makes the HTTP transcript
/// report `active_turn_started_at` / `has_pending_tool_calls: true` for a turn
/// that is long gone.
///
/// A native client that simply believes that snapshot hangs forever behind a
/// stop button the server answers with "No active task to stop." — the exact
/// symptom this class exists to prevent. The WebUI solves the same problem with
/// a completed-turn fence (`canonicalTurnWillSettle`,
/// `canReconcileCanonicalCompletion`, `COMPLETED_TURN_FENCE_MAX` in
/// `webui/src/lib/nanobot-client.ts`); this is the native equivalent, kept
/// small and pure so it can be tested on its own.
class TurnFence {
  /// Server turn ids this client has proven are gone.
  final Set<String> _fenced = <String>{};

  /// True once the socket released a `goal_status: running` claim that nothing
  /// ever corroborated (a ghost run). Until a real frame proves otherwise, a
  /// snapshot claiming an active turn is not believed.
  bool ghostRunReleased = false;

  /// Whether [serverTurnId] from a canonical transcript snapshot may set the
  /// busy/live state.
  ///
  /// [socketActive] is the socket's own view of the chat and always wins: the
  /// socket only holds a turn active while frames for it keep arriving (or a
  /// send of ours is in flight), so it cannot be fooled by the replay.
  bool shouldBelieve(String? serverTurnId, {required bool socketActive}) {
    if (serverTurnId == null || serverTurnId.isEmpty) return false;
    if (socketActive) {
      // The run is demonstrably alive: nothing may fence it.
      ghostRunReleased = false;
      _fenced.remove(serverTurnId);
      return true;
    }
    if (ghostRunReleased || _fenced.contains(serverTurnId)) {
      // Remember it so no later fetch re-adopts it.
      _fenced.add(serverTurnId);
      return false;
    }
    // A run that started before this client existed (opened from a
    // notification, restored after a kill): believe it, the socket will
    // release it if it turns out to be a ghost.
    return true;
  }

  /// A ghost run was released: from now on a snapshot's active turn needs the
  /// socket's backing.
  void noteGhostRun() => ghostRunReleased = true;

  /// A real turn frame arrived: the server is running this chat's turn, so any
  /// fence against it is void.
  void noteServedFrame() {
    ghostRunReleased = false;
    _fenced.clear();
  }

  /// Turn ids currently fenced. Exposed for assertions and diagnostics.
  Set<String> get fencedTurnIds => Set<String>.unmodifiable(_fenced);
}
