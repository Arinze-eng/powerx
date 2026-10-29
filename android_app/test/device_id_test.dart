import 'package:flutter_test/flutter_test.dart';
import 'package:powerx_android/services/device_id.dart';

void main() {
  group('Android signup fingerprint', () {
    test('different ANDROID_IDs give different fingerprints', () {
      final a = fingerprintOf(androidIdentityVector('9f3c1a2b7d4e5f60'));
      final b = fingerprintOf(androidIdentityVector('0a1b2c3d4e5f6071'));
      expect(a, isNot(b));
    });

    test('same ANDROID_ID is stable across calls', () {
      expect(
        fingerprintOf(androidIdentityVector('9f3c1a2b7d4e5f60')),
        fingerprintOf(androidIdentityVector(' 9f3c1a2b7d4e5f60 ')),
      );
    });

    test('ANDROID_ID is not mixed with firmware build fields', () {
      // Two phones with the same firmware build but different ANDROID_IDs
      // must not collide; the vector carries only the ANDROID_ID.
      expect(androidIdentityVector('abc'), 'android-id:abc');
    });

    test('missing ANDROID_ID falls back to a fresh secure random id', () {
      final x = androidIdentityVector(null);
      final y = androidIdentityVector('   ');
      expect(x, startsWith('rand:'));
      expect(y, startsWith('rand:'));
      expect(x, isNot(y));
      expect(x.length, 'rand:'.length + 64);
    });
  });
}
