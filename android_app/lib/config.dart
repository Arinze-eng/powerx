/// Build/runtime configuration for the PowerX native client.
class PowerXConfig {
  /// Base URL of the deployed PowerX (nanobot) gateway.
  /// Override at build time: --dart-define=POWERX_URL=https://your-host
  static const String baseUrl = String.fromEnvironment(
    'POWERX_URL',
    defaultValue: 'https://http--powerx--mxq9vl6k966n.code.run',
  );

  static const String appName = 'PowerX';
  static const String tagline = 'Your AI Work Partner';

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
