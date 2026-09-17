// Guards the Android build configuration against regressions that only surface
// inside Gradle — i.e. failures the Dart analyzer and widget tests cannot see,
// so the APK workflow dies at `flutter build apk` instead of at `flutter test`.
//
// Two historical breaks are pinned here:
//
//  1. minSdk floor. `record_android` (=1.5.2) declares minSdk 23 in its
//     manifest. When the app declared 21 the manifest merger hard-failed:
//       "uses-sdk:minSdkVersion 21 cannot be smaller than version 23 declared
//        in library [:record_android]"
//     so the whole release APK build aborted.
//
//  2. The `record_linux` dependency override. Without it pub resolved
//     record_linux 0.7.2, which does not implement `startStream` and still had
//     the old `hasPermission` signature. The host tool compiles every reachable
//     federated plugin source, so the Android kernel snapshot failed to compile
//     even though the plugin is Linux-only.
//
// These are cheap file assertions, but they run in the normal `flutter test`
// step, which means a regression fails fast and loudly instead of costing a
// full Gradle cycle to discover.

import 'dart:io';

import 'package:flutter_test/flutter_test.dart';

/// Minimum Android API level the manifest merger will accept.
///
/// Keep in sync with the highest `minSdk` declared by any resolved plugin.
/// Today the binding constraint is `record_android` (minSdk 23).
const int kRequiredAndroidMinSdk = 23;

void main() {
  group('Android build configuration', () {
    late String gradleConfig;
    late String pubspec;

    setUpAll(() {
      final gradleFile = File('android/app/build.gradle.kts');
      final pubspecFile = File('pubspec.yaml');
      expect(
        gradleFile.existsSync(),
        isTrue,
        reason: 'Expected android/app/build.gradle.kts to exist',
      );
      expect(pubspecFile.existsSync(), isTrue, reason: 'Expected pubspec.yaml to exist');
      gradleConfig = gradleFile.readAsStringSync();
      pubspec = pubspecFile.readAsStringSync();
    });

    test('minSdk is at least $kRequiredAndroidMinSdk (record_android floor)', () {
      final match = RegExp(r'minSdk\s*=\s*(\d+)').firstMatch(gradleConfig);
      expect(
        match,
        isNotNull,
        reason: 'Could not find a "minSdk = <n>" declaration in '
            'android/app/build.gradle.kts',
      );

      final minSdk = int.parse(match!.group(1)!);
      expect(
        minSdk,
        greaterThanOrEqualTo(kRequiredAndroidMinSdk),
        reason: 'minSdk $minSdk is below the $kRequiredAndroidMinSdk floor required '
            'by the voice-note plugin stack (record_android). The Android manifest '
            'merger will fail the release build with '
            '"uses-sdk:minSdkVersion $minSdk cannot be smaller than version '
            '$kRequiredAndroidMinSdk declared in library [:record_android]".',
      );
    });

    test('record_linux override keeps the federated plugins in sync', () {
      // The override must live under `dependency_overrides:`, not the regular
      // dependency list — otherwise it would be treated as a direct dependency
      // and could be silently dropped.
      expect(
        pubspec,
        contains('dependency_overrides:'),
        reason: 'pubspec.yaml must declare a dependency_overrides section so the '
            'record_linux patch can be applied',
      );

      final overrideMatch =
          RegExp(r'dependency_overrides:([\s\S]*)').firstMatch(pubspec);
      expect(overrideMatch, isNotNull);
      expect(
        overrideMatch!.group(1),
        contains('record_linux:'),
        reason: 'The record_linux override is missing. Without it pub resolves '
            'record_linux 0.7.2, whose stale hasPermission/startStream signatures '
            'break the Dart kernel snapshot compile for every platform target — '
            'including Android APK builds.',
      );
    });

    test('the microphone permission the recorder depends on is declared', () {
      final manifest = File('android/app/src/main/AndroidManifest.xml');
      expect(manifest.existsSync(), isTrue);

      final contents = manifest.readAsStringSync();
      expect(
        contents,
        contains('android.permission.RECORD_AUDIO'),
        reason: 'Voice notes capture audio, so RECORD_AUDIO must be declared. '
            'Without it the permission request is auto-denied and the mic button '
            'silently does nothing.',
      );
    });
  });
}