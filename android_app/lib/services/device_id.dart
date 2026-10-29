import 'dart:convert';
import 'dart:io';
import 'dart:math';

import 'package:crypto/crypto.dart';
import 'package:device_info_plus/device_info_plus.dart';
import 'package:flutter/services.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';

/// Identity vector for an Android install, built from Settings.Secure.ANDROID_ID
/// (read natively in MainActivity). device_info_plus `androidInfo.id` is
/// Build.ID, a firmware label shared by every phone on the same build, so it
/// must not be used here. When ANDROID_ID is unavailable a per-install secure
/// random id is used instead.
String androidIdentityVector(String? androidId) {
  final id = androidId?.trim() ?? '';
  if (id.isNotEmpty) return 'android-id:$id';
  return 'rand:${_secureRandomHex()}';
}

/// sha256 hex of an identity vector.
String fingerprintOf(String rawIdentity) =>
    sha256.convert(utf8.encode(rawIdentity)).toString();

String _secureRandomHex() {
  final rng = Random.secure();
  final bytes = List<int>.generate(32, (_) => rng.nextInt(256));
  return bytes.map((b) => b.toRadixString(16).padLeft(2, '0')).join();
}

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
  static const _deviceChannel = MethodChannel('powerx/device');
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
    final fp = fingerprintOf(raw);
    await _storage.write(key: _fpKey, value: fp);
    _cached = fp;
    return fp;
  }

  Future<String> _identityVector() async {
    if (Platform.isAndroid) {
      return androidIdentityVector(await _androidId());
    }
    try {
      if (Platform.isIOS) {
        final info = await DeviceInfoPlugin().iosInfo;
        final joined = [info.identifierForVendor ?? '', info.model, info.name]
            .where((p) => p.trim().isNotEmpty)
            .join('|');
        if (joined.trim().isNotEmpty) return 'ios:$joined';
      }
    } catch (_) {
      // Fall through to the random fallback below.
    }
    return 'rand:${_secureRandomHex()}';
  }

  /// Settings.Secure.ANDROID_ID via MainActivity; null if unavailable.
  Future<String?> _androidId() async {
    try {
      final id = await _deviceChannel.invokeMethod<String>('androidId');
      return (id == null || id.trim().isEmpty) ? null : id.trim();
    } catch (_) {
      return null;
    }
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