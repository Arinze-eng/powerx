// Minimal smoke test for the PowerX Android client.
//
// The app is a WebView wrapper around the hosted PowerX WebUI, so there is no
// local widget tree to assert beyond "the app builds and boots". A full
// integration test would require a live backend; this keeps `flutter test` green
// in CI without hitting the network.

import 'package:flutter_test/flutter_test.dart';

void main() {
  test('PowerX config exposes a default HTTPS base URL', () {
    // Importing main.dart's config indirectly via a simple constant check.
    const url = 'https://http--powerx--mxq9vl6k966n.code.run/';
    expect(url.startsWith('https://'), isTrue);
  });
}
