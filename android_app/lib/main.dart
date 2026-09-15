import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_inappwebview/flutter_inappwebview.dart';
import 'package:http/http.dart' as http;
import 'package:open_filex/open_filex.dart';
import 'package:path_provider/path_provider.dart';
import 'package:url_launcher/url_launcher.dart';

/// PowerX Android client — a *native-looking* wrapper around the self-hosted
/// PowerX AI agent (nanobot gateway).
///
/// Design goal: nobody should be able to tell this is a WebView. There is no
/// browser chrome, no reload button, no visible URL, no web pull-to-refresh,
/// no text-selection handles, no pinch-zoom, and no right-click menu. A branded
/// splash covers the cold start, then the hosted WebUI renders full-bleed under
/// an edge-to-edge immersive layout so it reads as a first-class app screen.
///
/// Backend URL is overridable at build time:
///   flutter build apk --dart-define=POWERX_URL=https://your-host
void main() {
  WidgetsFlutterBinding.ensureInitialized();
  SystemChrome.setEnabledSystemUIMode(
    SystemUiMode.edgeToEdge,
    overlays: [SystemUiOverlay.top, SystemUiOverlay.bottom],
  );
  runApp(const PowerXApp());
}

class PowerXConfig {
  static const String baseUrl = String.fromEnvironment(
    'POWERX_URL',
    defaultValue: 'https://http--powerx--mxq9vl6k966n.code.run/',
  );
  static const String appName = 'PowerX';
}

class PowerXApp extends StatelessWidget {
  const PowerXApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: PowerXConfig.appName,
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        useMaterial3: true,
        brightness: Brightness.dark,
        scaffoldBackgroundColor: const Color(0xFF0B1020),
        colorScheme: ColorScheme.fromSeed(
          seedColor: const Color(0xFF2E7D32),
          brightness: Brightness.dark,
        ),
      ),
      home: const PowerXHomeScreen(),
    );
  }
}

class PowerXHomeScreen extends StatefulWidget {
  const PowerXHomeScreen({super.key});

  @override
  State<PowerXHomeScreen> createState() => _PowerXHomeScreenState();
}

class _PowerXHomeScreenState extends State<PowerXHomeScreen> {
  final GlobalKey webViewKey = GlobalKey();
  InAppWebViewController? _controller;
  bool _ready = false; // page loaded enough to reveal (hides splash)

  Uri get _baseUri => Uri.parse(PowerXConfig.baseUrl);

  bool _isSameOrigin(String? url) {
    if (url == null) return false;
    try {
      return Uri.parse(url).host == _baseUri.host;
    } catch (_) {
      return false;
    }
  }

  Future<void> _openExternal(String url) async {
    final uri = Uri.tryParse(url);
    if (uri == null) return;
    if (await canLaunchUrl(uri)) {
      await launchUrl(uri, mode: LaunchMode.externalApplication);
    }
  }

  /// CSS + JS injected on every load to erase all "this is a browser" signals
  /// and give the hosted UI a tight native feel.
  static const String _stealthCss = '''
    *, *::before, *::after {
      -webkit-touch-callout: none !important;
    }
    html, body {
      overscroll-behavior: none !important;
      overflow-x: hidden !important;
      touch-action: manipulation !important;
      -webkit-user-select: none !important;
      user-select: none !important;
    }
    /* Keep inputs/textareas usable — users must still type & copy chat text. */
    input, textarea, [contenteditable="true"], .selectable, pre, code,
    .markdown-body, .prose, [data-selectable="true"] {
      -webkit-user-select: text !important;
      user-select: text !important;
    }
    ::-webkit-scrollbar { width: 0px !important; height: 0px !important;
      background: transparent !important; }
    img { -webkit-user-drag: none !important; pointer-events: auto; }
    a { -webkit-tap-highlight-color: transparent !important; }
  ''';

  String get _stealthJs {
    // jsonEncode safely embeds the CSS as a JS string literal (escapes quotes).
    final cssLit = jsonEncode(_stealthCss);
    return '(function(){'
        'var s=document.getElementById("__powerx_stealth__");'
        'if(!s){s=document.createElement("style");s.id="__powerx_stealth__";'
        'document.documentElement.appendChild(s);}'
        's.textContent=$cssLit;'
        'window.addEventListener("contextmenu",function(e){e.preventDefault();},true);'
        'document.addEventListener("dragstart",function(e){e.preventDefault();},true);'
        'var vp=document.querySelector(\'meta[name="viewport"]\');'
        'var c="width=device-width, initial-scale=1.0, maximum-scale=1.0, '
        'user-scalable=no, viewport-fit=cover";'
        'if(!vp){vp=document.createElement("meta");vp.name="viewport";'
        'document.head.appendChild(vp);}'
        'vp.setAttribute("content",c);'
        '})();';
  }

  Future<void> _injectStealth() async {
    final c = _controller;
    if (c == null) return;
    try {
      await c.evaluateJavascript(source: _stealthJs);
    } catch (_) {}
  }

  Future<void> _handleDownload(DownloadStartRequest download) async {
    final dir = await getExternalStorageDirectory();
    final fileName = (download.suggestedFilename ?? '').isNotEmpty
        ? download.suggestedFilename!
        : 'powerx-file';
    final savePath = '${dir?.path ?? '.'}/$fileName';
    try {
      final resp = await http.get(download.url);
      if (resp.statusCode != 200) throw Exception('download failed');
      final f = File(savePath);
      await f.create(recursive: true);
      await f.writeAsBytes(resp.bodyBytes);
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text('Saved $fileName'),
          action: SnackBarAction(label: 'Open', onPressed: () => OpenFilex.open(savePath)),
        ),
      );
    } catch (_) {
      unawaited(_openExternal(download.url.toString()));
    }
  }

  void _onLoadStop(String? url) {
    _injectStealth();
    if (mounted && !_ready) setState(() => _ready = true);
  }

  @override
  Widget build(BuildContext context) {
    final navigator = Navigator.of(context);
    return PopScope(
      canPop: false,
      onPopInvokedWithResult: (didPop, result) async {
        if (didPop) return;
        final c = _controller;
        if (c != null && await c.canGoBack()) {
          await c.goBack();
        } else {
          // At the root of the app → allow normal exit.
          if (!mounted) return;
          navigator.maybePop();
        }
      },
      child: Scaffold(
        backgroundColor: const Color(0xFF0B1020),
        body: Stack(
          children: [
            Positioned.fill(
              child: Theme(
                data: Theme.of(context).copyWith(splashFactory: NoSplash.splashFactory),
                child: InAppWebView(
                  key: webViewKey,
                  initialUrlRequest: URLRequest(url: WebUri(PowerXConfig.baseUrl)),
                  initialSettings: InAppWebViewSettings(
                    javaScriptEnabled: true,
                    domStorageEnabled: true,
                    databaseEnabled: true,
                    safeBrowsingEnabled: false,
                    useShouldOverrideUrlLoading: true,
                    useOnDownloadStart: true,
                    allowsInlineMediaPlayback: true,
                    mediaPlaybackRequiresUserGesture: false,
                    // Native-feel rendering knobs:
                    clearCache: false,
                    cacheEnabled: true,
                    textZoom: 100,
                    builtInZoomControls: false,
                    displayZoomControls: false,
                    useWideViewPort: false,
                    loadWithOverviewMode: false,
                    supportZoom: false,
                    disableHorizontalScroll: true,
                    disableVerticalScroll: false,
                  ),
                  onWebViewCreated: (c) => _controller = c,
                  onLoadStart: (c, url) => _injectStealth(),
                  onLoadStop: (c, url) => _onLoadStop(url?.toString()),
                  shouldOverrideUrlLoading: (c, action) async {
                    final url = action.request.url?.toString();
                    if (url == null) return NavigationActionPolicy.ALLOW;
                    if (_isSameOrigin(url)) return NavigationActionPolicy.ALLOW;
                    unawaited(_openExternal(url));
                    return NavigationActionPolicy.CANCEL;
                  },
                  onDownloadStartRequest: (c, d) => _handleDownload(d),
                  onCreateWindow: (c, attr) async {
                    final url = attr.request.url?.toString();
                    if (url != null && !_isSameOrigin(url)) {
                      unawaited(_openExternal(url));
                    }
                    return false;
                  },
                ),
              ),
            ),
            // Branded native splash that fully covers the cold-start web load.
            AnimatedOpacity(
              duration: const Duration(milliseconds: 450),
              curve: Curves.easeOut,
              opacity: _ready ? 0 : 1,
              child: IgnorePointer(ignoring: _ready, child: const _NativeSplash()),
            ),
          ],
        ),
      ),
    );
  }
}

/// A polished, app-branded loading screen shown until the hosted UI is ready.
/// Deliberately generic so it never reveals anything about the underlying tech.
class _NativeSplash extends StatefulWidget {
  const _NativeSplash();
  @override
  State<_NativeSplash> createState() => _NativeSplashState();
}

class _NativeSplashState extends State<_NativeSplash>
    with SingleTickerProviderStateMixin {
  late final AnimationController _pulse;
  @override
  void initState() {
    super.initState();
    _pulse = AnimationController(
        vsync: this, duration: const Duration(milliseconds: 1100))
      ..repeat(reverse: true);
  }

  @override
  void dispose() {
    _pulse.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Container(
      color: const Color(0xFF0B1020),
      alignment: Alignment.center,
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          Container(
            width: 88,
            height: 88,
            decoration: BoxDecoration(
              gradient: const LinearGradient(
                colors: [Color(0xFF2E7D32), Color(0xFF66BB6A)],
                begin: Alignment.topLeft,
                end: Alignment.bottomRight,
              ),
              borderRadius: BorderRadius.circular(22),
              boxShadow: [
                BoxShadow(
                  color: const Color(0xFF2E7D32).withValues(alpha: 0.45),
                  blurRadius: 30,
                  spreadRadius: 2,
                )
              ],
            ),
            child: const Center(
              child: Text('⚡', style: TextStyle(fontSize: 44)),
            ),
          ),
          const SizedBox(height: 22),
          const Text(
            PowerXConfig.appName,
            style: TextStyle(
              fontSize: 26,
              fontWeight: FontWeight.w800,
              letterSpacing: 0.5,
              color: Colors.white,
            ),
          ),
          const SizedBox(height: 6),
          const Text(
            'Your AI Work Partner',
            style: TextStyle(fontSize: 13, color: Colors.white54),
          ),
          const SizedBox(height: 30),
          FadeTransition(
            opacity: Tween(begin: 0.35, end: 1.0).animate(_pulse),
            child: const SizedBox(
              width: 26,
              height: 26,
              child: CircularProgressIndicator(
                strokeWidth: 2.5,
                valueColor: AlwaysStoppedAnimation(Color(0xFF66BB6A)),
              ),
            ),
          ),
        ],
      ),
    );
  }
}
