import 'dart:math';

final Random _secureRandom = Random.secure();

/// A random RFC 4122 v4 UUID for one user turn.
///
/// The client names every turn it submits (WebUI parity: `crypto.randomUUID()`
/// in useNanobotStream). The gateway only tags its run wall-clock, the
/// `message_accepted` ack and the `goal_status: running` replay with an id it
/// was given, so without one a reopened task cannot be corroborated and the
/// app drops its busy state and live progress.
String newTurnId() {
  final b = List<int>.generate(16, (_) => _secureRandom.nextInt(256));
  b[6] = (b[6] & 0x0f) | 0x40;
  b[8] = (b[8] & 0x3f) | 0x80;
  String hex(int from, int to) => b
      .sublist(from, to)
      .map((x) => x.toRadixString(16).padLeft(2, '0'))
      .join();
  return '${hex(0, 4)}-${hex(4, 6)}-${hex(6, 8)}-${hex(8, 10)}-${hex(10, 16)}';
}
