// Sign-out must return to the root route. Pushing a new '/auth' route in its
// place removed _RootGate from the navigator, so the recovery-code step and
// the sign-in/sign-up result were never rendered until the app restarted.
// Cheap source assertion, runs in the normal `flutter test` step.

import 'dart:io';

import 'package:flutter_test/flutter_test.dart';

void main() {
  test('sign-out pops to the root gate instead of pushing a new /auth route',
      () {
    final src = File('lib/screens/settings_screen.dart').readAsStringSync();
    expect(src, isNot(contains("pushNamedAndRemoveUntil('/auth'")),
        reason: 'removing the root gate hides the recovery-code step and '
            'the post-sign-in Home until a restart');
    expect(src, contains('popUntil((r) => r.isFirst)'));
  });
}
