import 'dart:convert';
import 'dart:io';

import 'package:crypto/crypto.dart';
import 'package:device_info_plus/device_info_plus.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';

/// Stable per-device signup fingerprint + local "already signed up" lock.
///
/// The fingerprint is sha256 of a hardware/OS identity vector and is stored in
/// secure storage the first time it is computed, so it stays stable for the
/// life of the install even if a platform identifier rotates. The server
/// (signup-gate edge function) is the enforcement authority; this value is
/// what binds the device to the server-side anti-abuse record.
class DeviceId {
  DeviceId._();
  static final DeviceId instance = DeviceId._();

  static const _fpKey = 'device.fingerprint.v1';
  static const _signedUpKey = 'device.signup.lock';
  static const _accountKey = 'device.signup.email';

  final FlutterSecureStorage _storage = const FlutterSecureStorage(
      aOptions: AndroidOptions(encryptedSharedPreferences: true));
  String? _cached;

  /// Stable fingerprint for this install. Falls back to a persisted random id
  /// when hardware identifiers are unavailable (never returns empty).
  Future<String> fingerprint() async {
    if (_cached != null) return _cached!;
    final stored = await _storage.read(key: _fpKey);
    if (stored != null && stored.isNotEmpty) {
      _cached = stored;
      return stored;
    }
    final raw = await _identityVector();
    final fp = sha256.convert(utf8.encode(raw)).toString();
    await _storage.write(key: _fpKey, value: fp);
    _cached = fp;
    return fp;
  }

  Future<String> _identityVector() async {
    try {
      if (Platform.isAndroid) {
        final info = await DeviceInfoPlugin().androidInfo;
        final parts = [
          info.id,
          info.brand,
          info.model,
          info.device,
          info.manufacturer,
          info.version.release,
          info.version.sdkInt.toString(),
        ];
        final joined = parts.where((p) => p.trim().isNotEmpty).join('|');
        if (joined.trim().isNotEmpty) return 'android:$joined';
      } else if (Platform.isIOS) {
        final info = await DeviceInfoPlugin().iosInfo;
        final joined = [info.identifierForVendor ?? '', info.model, info.name]
            .where((p) => p.trim().isNotEmpty)
            .join('|');
        if (joined.trim().isNotEmpty) return 'ios:$joined';
      }
    } catch (_) {
      // Fall through to the random fallback below.
    }
    // Random fallback persisted per install (unique, just not hardware-bound).
    var rand = await _storage.read(key: _fpKey);
    if (rand == null || rand.isEmpty) {
      final bytes = List<int>.generate(32, (_) => DateTime.now().microsecondsSinceEpoch % 256 + DateTime.now().millisecondsSinceEpoch % 97);
      rand = sha256.convert(bytes).toString();
    }
    return 'rand:$rand';
  }

  /// True if an account was created on this device before (local best-effort
  /// mirror of the server-side lock — works even while offline).
  Future<bool> signedUpLocally() async {
    final v = await _storage.read(key: _signedUpKey);
    return v == '1';
  }

  /// Local mirror: record that signup succeeded on this device.
  Future<void> markSignedUp(String email) async {
    await _storage.write(key: _signedUpKey, value: '1');
    await _storage.write(key: _accountKey, value: email.trim().toLowerCase());
  }

  /// The email bound locally to this device (best-effort, may be null).
  Future<String?> boundEmail() => _storage.read(key: _accountKey);

  /// Clears the local lock (e.g. after a successful server-side check proves
  /// the device was never used). Only called internally in tests.
  Future<void> clearLocalLock() async {
    await _storage.delete(key: _signedUpKey);
    await _storage.delete(key: _accountKey);
  }
}