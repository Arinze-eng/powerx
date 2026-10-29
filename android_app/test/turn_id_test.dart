// The client names each submitted turn with a v4 UUID (WebUI parity), so the
// gateway's run wall-clock, `message_accepted` ack and replays carry an id the
// reopened app can corroborate.

import 'package:flutter_test/flutter_test.dart';
import 'package:powerx_android/utils/turn_id.dart';

void main() {
  test('newTurnId is a v4 UUID', () {
    final re = RegExp(
        r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$');
    for (var i = 0; i < 50; i++) {
      expect(newTurnId(), matches(re));
    }
  });

  test('newTurnId values do not repeat', () {
    final seen = <String>{};
    for (var i = 0; i < 500; i++) {
      seen.add(newTurnId());
    }
    expect(seen.length, 500);
  });
}
