import 'dart:async';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_inappwebview/flutter_inappwebview.dart';
import 'package:http/http.dart' as http;
import 'package:open_filex/open_filex.dart';
import 'package:path_provider/path_provider.dart';
import 'package:url_launcher/url_launcher.dart';

/// PowerX Android client.
///
/// A single-screen WebView wrapper that loads the self-hosted PowerX WebUI
/// (nanobot gateway). All authentication (Supabase) and chat happen inside the
/// hosted web app itself, so this shell only needs to render it reliably and
/// handle file downloads / external links.
///
/// The backend URL is overridable at build time:
///   flutter build apk --dart-define=POWERX_URL=https://your-host
void main() {
  WidgetsFlutterBinding.ensureInitialized();
  runApp(const PowerXApp());
}

class PowerXConfig {
  /// Base URL of the deployed PowerX (nanobot) service.
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
        colorScheme: ColorScheme.fromSeed(
          seedColor: const Color(0xFF2E7D32),
          brightness: Brightness.dark,
        ),
        scaffoldBackgroundColor: const Color(0xFF0B1020),
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
  bool _loading = true;
  double _progress = 0;

  // Keep screen awake while chatting.
  @override
  void initState() {
    super.initState();
    SystemChrome.setEnabledSystemUIMode(SystemUiMode.edgeToEdge);
    _initPlatform();
  }

  Future<void> _initPlatform() async {
    if (Platform.isAndroid) {
      await InAppWebViewController.setWebContentsDebuggingEnabled(false);
    }
  }

  bool _isSameOrigin(String? url) {
    if (url == null) return false;
    try {
      final base = Uri.parse(PowerXConfig.baseUrl);
      final target = Uri.parse(url);
      return target.host == base.host;
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

  Future<void> _handleDownload(DownloadStartRequest download) async {
    final dir = await getExternalStorageDirectory();
    final fileName = (download.suggestedFilename ?? '').isNotEmpty
        ? download.suggestedFilename!
        : 'powerx-file';
    final savePath = '${dir?.path ?? '.'}/$fileName';
    try {
      final resp = await http.get(Uri.parse(download.url.toString()));
      if (resp.statusCode != 200) throw Exception('download failed');
      final f = File(savePath);
      await f.create(recursive: true);
      await f.writeAsBytes(resp.bodyBytes);
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text('Saved $fileName'),
          action: SnackBarAction(
            label: 'Open',
            onPressed: () => OpenFilex.open(savePath),
          ),
        ),
      );
    } catch (_) {
      // Fallback: open in system browser so the user still gets the file.
      unawaited(_openExternal(download.url.toString()));
    }
  }

  Future<void> _refresh() async {
    await _controller?.reload();
  }

  @override
  Widget build(BuildContext context) {
    return PopScope(
      canPop: false,
      onPopInvokedWithResult: (didPop, result) async {
        if (didPop) return;
        if (_controller != null && await _controller!.canGoBack()) {
          await _controller!.goBack();
        }
      },
      child: Scaffold(
        appBar: AppBar(
          title: const Row(
            children: [
              Text('⚡', style: TextStyle(fontSize: 20)),
              SizedBox(width: 8),
              Text(PowerXConfig.appName,
                  style: TextStyle(fontWeight: FontWeight.w800)),
            ],
          ),
          actions: [
            IconButton(
              tooltip: 'Reload',
              icon: const Icon(Icons.refresh),
              onPressed: _refresh,
            ),
          ],
          bottom: _loading
              ? PreferredSize(
                  preferredSize: const Size.fromHeight(3),
                  child: LinearProgressIndicator(
                    value: _progress > 0 ? _progress / 100 : null,
                    minHeight: 3,
                    backgroundColor: Colors.transparent,
                  ),
                )
              : null,
        ),
        body: SafeArea(
          top: false,
          child: Stack(
            children: [
              InAppWebView(
                key: webViewKey,
                initialUrlRequest:
                    URLRequest(url: WebUri(PowerXConfig.baseUrl)),
                initialSettings: InAppWebViewSettings(
                  javaScriptEnabled: true,
                  domStorageEnabled: true,
                  databaseEnabled: true,
                  useShouldOverrideUrlLoading: true,
                  useOnDownloadStart: true,
                  allowsInlineMediaPlayback: true,
                  mediaPlaybackRequiresUserGesture: false,
                  safeBrowsingEnabled: true,
                ),
                onWebViewCreated: (c) {
                  _controller = c;
                },
                onProgressChanged: (c, progress) {
                  setState(() => _progress = progress.toDouble());
                },
                onLoadStart: (c, url) {
                  if (mounted) setState(() => _loading = true);
                },
                onLoadStop: (c, url) {
                  if (mounted) setState(() => _loading = false);
                },
                shouldOverrideUrlLoading: (c, action) async {
                  final url = action.request.url?.toString();
                  if (url == null) {
                    return NavigationActionPolicy.ALLOW;
                  }
                  if (_isSameOrigin(url)) {
                    return NavigationActionPolicy.ALLOW;
                  }
                  // External link → open in the system browser.
                  unawaited(_openExternal(url));
                  return NavigationActionPolicy.CANCEL;
                },
                onDownloadStartRequest: (c, download) async {
                  await _handleDownload(download);
                },
                onCreateWindow: (c, attr) async {
                  final url = attr.request.url?.toString();
                  if (url != null && !_isSameOrigin(url)) {
                    unawaited(_openExternal(url));
                  }
                  return false;
                },
              ),
              if (_loading)
                const Center(
                  child: Column(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      SizedBox(
                        width: 40,
                        height: 40,
                        child: CircularProgressIndicator(strokeWidth: 3),
                      ),
                      SizedBox(height: 14),
                      Text('Connecting to PowerX…',
                          style: TextStyle(color: Colors.white70)),
                    ],
                  ),
                ),
            ],
          ),
        ),
      ),
    );
  }
}
