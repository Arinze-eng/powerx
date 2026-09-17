/// Build/runtime configuration for the CDNAI native client (PowerX engine).
class PowerXConfig {
  /// Base URL of the deployed CDNAI (nanobot) gateway.
  /// Override at build time: --dart-define=POWERX_URL=https://your-host
  static const String baseUrl = String.fromEnvironment(
    'POWERX_URL',
    defaultValue: 'https://http--powerx--mxq9vl6k966n.code.run',
  );

  /// User-visible app name. Keep in sync with pubspec `version` below — the
  /// Settings > About row is the install-time proof of which build is running.
  static const String appName = 'CDNAI';
  static const String tagline = 'Your AI Work Partner';
  static const String appVersion = '1.4.0+10';

  /// Normalized base without trailing slash.
  static String get origin {
    var u = baseUrl.trim();
    while (u.endsWith('/')) {
      u = u.substring(0, u.length - 1);
    }
    return u;
  }

  /// WebSocket origin (https->wss, http->ws) derived from [origin].
  static String get wsOrigin {
    final o = origin;
    if (o.startsWith('https://')) {
      return 'wss://${o.substring('https://'.length)}';
    }
    if (o.startsWith('http://')) {
      return 'ws://${o.substring('http://'.length)}';
    }
    return o;
  }
}
