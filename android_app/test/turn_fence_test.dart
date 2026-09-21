// The canonical-run fence: which `active turn` a transcript snapshot may
// believe. Guards the "app hangs forever with results missing after a long
// task" bug, where the gateway replays a killed run's wall-clock on every
// attach and the client re-adopted it from every transcript fetch.

import 'package:flutter_test/flutter_test.dart';
import 'package:powerx_android/utils/turn_fence.dart';

void main() {
  group('TurnFence', () {
    test('a snapshot active turn is believed while nothing has gone wrong', () {
      final f = TurnFence();
      expect(f.shouldBelieve('turn-1', socketActive: false), isTrue);
      expect(f.fencedTurnIds, isEmpty);
    });

    test('no active turn is never believed', () {
      final f = TurnFence();
      expect(f.shouldBelieve(null, socketActive: false), isFalse);
      expect(f.shouldBelieve('', socketActive: false), isFalse);
    });

    test('after a ghost run the snapshot id is fenced, on every fetch', () {
      final f = TurnFence();
      f.noteGhostRun();
      expect(f.shouldBelieve('turn-1', socketActive: false), isFalse);
      expect(f.fencedTurnIds, contains('turn-1'));
      // Re-fetching must not walk the UI back into a permanent busy state.
      expect(f.shouldBelieve('turn-1', socketActive: false), isFalse);
    });

    test('a socket-backed run always wins over the fence', () {
      final f = TurnFence();
      f.noteGhostRun();
      expect(f.shouldBelieve('turn-1', socketActive: true), isTrue);
      expect(f.ghostRunReleased, isFalse,
          reason: 'a demonstrably live run clears the ghost flag');
    });

    test('a real turn frame voids the fence so a live run is never suppressed',
        () {
      final f = TurnFence();
      f.noteGhostRun();
      f.shouldBelieve('gone', socketActive: false);
      f.noteServedFrame();
      expect(f.fencedTurnIds, isEmpty);
      expect(f.shouldBelieve('turn-2', socketActive: false), isTrue);
    });
  });
}
