import 'package:flutter/material.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';

import '../theme/app_theme.dart';
import '../theme/tokens.dart';

/// Owns the app's colour-scheme choice.
///
/// Mirrors `webui/src/hooks/useTheme.ts`:
///
/// * the default is **the system setting** (`prefers-color-scheme` on the web,
///   the phone's Dark theme switch on Android);
/// * once the user touches the sun/moon control the choice is pinned and
///   persisted, so it survives a restart exactly like `localStorage` on the web;
/// * `toggle()` flips whatever the *resolved* scheme currently is, so one tap
///   always produces the opposite of what is on screen.
class ThemeController extends ChangeNotifier with WidgetsBindingObserver {
  ThemeController({FlutterSecureStorage? storage, bool observeBinding = true})
    : _storage = storage ?? const FlutterSecureStorage() {
    if (observeBinding) {
      WidgetsBinding.instance.addObserver(this);
      _platformBrightness = WidgetsBinding.instance.platformDispatcher.platformBrightness;
    }
  }

  static const storageKey = 'theme_mode';

  final FlutterSecureStorage _storage;

  ThemeMode _mode = ThemeMode.system;
  Brightness _platformBrightness = Brightness.light;
  bool _loaded = false;

  ThemeMode get mode => _mode;

  /// False until the persisted choice has been read; the UI can stay quiet
  /// rather than flashing the wrong theme for one frame.
  bool get loaded => _loaded;

  Brightness get platformBrightness => _platformBrightness;

  /// The scheme that is actually on screen right now.
  bool get isDark => AppTheme.isDark(_mode, _platformBrightness);

  /// The palette currently in force, resolved for [mode] + system setting.
  WebPalette get palette => AppTheme.paletteFor(_mode, _platformBrightness);

  Future<void> load() async {
    try {
      final stored = await _storage.read(key: storageKey);
      if (stored == 'light') _mode = ThemeMode.light;
      if (stored == 'dark') _mode = ThemeMode.dark;
      if (stored == 'system') _mode = ThemeMode.system;
    } catch (_) {
      // A locked keystore must not stop the app from starting.
    }
    _loaded = true;
    notifyListeners();
  }

  Future<void> setMode(ThemeMode mode) async {
    if (_mode == mode) return;
    _mode = mode;
    notifyListeners();
    try {
      await _storage.write(key: storageKey, value: _nameOf(mode));
    } catch (_) {}
  }

  /// Flip to the opposite of what the user is looking at.
  Future<void> toggle() => setMode(isDark ? ThemeMode.light : ThemeMode.dark);

  @override
  void didChangePlatformBrightness() {
    final next = WidgetsBinding.instance.platformDispatcher.platformBrightness;
    if (next == _platformBrightness) return;
    _platformBrightness = next;
    // Only matters while we are following the system.
    if (_mode == ThemeMode.system) notifyListeners();
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    super.dispose();
  }

  static String _nameOf(ThemeMode mode) {
    switch (mode) {
      case ThemeMode.light:
        return 'light';
      case ThemeMode.dark:
        return 'dark';
      case ThemeMode.system:
        return 'system';
    }
  }
}
