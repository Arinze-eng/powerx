import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:provider/provider.dart';

import 'config.dart';
import 'screens/auth_screen.dart';
import 'screens/home_screen.dart';
import 'screens/settings_screen.dart';
import 'state/app_state.dart';
import 'state/theme_controller.dart';
import 'theme/app_theme.dart';
import 'theme/palette.dart';
import 'theme/tokens.dart';
import 'widgets/brand.dart';

/// Global crash fence.
///
/// The previous build let a single uncaught exception in a background
/// callback (socket frame handling, file IO, a platform channel returning
/// null) tear down the whole Android process — which is exactly the "APK
/// keeps stopping" report, and why the task looked gone even though it was
/// still running server-side. Every isolate-level and framework-level error
/// is now swallowed and logged instead of killing the UI. The app stays
/// alive, the socket keeps streaming, and the transcript still lands.
void main() {
  // Framework errors (build/layout/paint, gesture callbacks) must never
  // terminate the process. Log in debug, ignore in release.
  FlutterError.onError = (FlutterErrorDetails details) {
    if (kDebugMode) {
      FlutterError.presentError(details);
    }
  };

  // Platform-dispatcher errors: anything thrown outside the framework's
  // zones (platform channels, plugin callbacks, timers). Returning true
  // marks the error as handled so the engine does not abort.
  PlatformDispatcher.instance.onError = (error, stack) {
    if (kDebugMode) {
      debugPrint('Unhandled platform error: $error');
    }
    return true;
  };

  // Last-resort net for async gaps that escape both handlers above.
  runZonedGuarded(
    () {
      WidgetsFlutterBinding.ensureInitialized();
      runApp(const PowerXApp());
    },
    (error, stack) {
      if (kDebugMode) {
        debugPrint('Unhandled zone error: $error');
      }
    },
  );
}

class PowerXApp extends StatelessWidget {
  const PowerXApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MultiProvider(
      providers: [
        ChangeNotifierProvider(create: (_) => ThemeController()..load()),
        ChangeNotifierProvider(create: (_) => AppState()..init()),
      ],
      child: const _App(),
    );
  }
}

/// The root widget below the providers: resolves the active scheme once and
/// hands it to both `MaterialApp` and the legacy [Palette] facade.
///
/// The facade is pointed at the resolved palette *before* the subtree builds,
/// which is what lets every screen that still reads `Palette.bg0` follow the
/// theme without each one being rewritten first.
class _App extends StatelessWidget {
  const _App();

  @override
  Widget build(BuildContext context) {
    final theme = context.watch<ThemeController>();
    final dark = theme.isDark;
    Palette.activate(dark ? WebPalette.dark : WebPalette.light);

    // Status/navigation bars follow the scheme, the way the web's
    // `meta[name=theme-color]` swap does.
    SystemChrome.setSystemUIOverlayStyle(
      SystemUiOverlayStyle(
        statusBarColor: Colors.transparent,
        systemNavigationBarColor: Palette.bg0,
        statusBarIconBrightness: dark ? Brightness.light : Brightness.dark,
        systemNavigationBarIconBrightness:
            dark ? Brightness.light : Brightness.dark,
      ),
    );

    return MaterialApp(
      title: PowerXConfig.appName,
      debugShowCheckedModeBanner: false,
      theme: AppTheme.light(),
      darkTheme: AppTheme.dark(),
      themeMode: theme.mode,
      home: const _RootGate(),
      routes: {
        '/auth': (_) => const AuthScreen(),
        '/home': (_) => const HomeScreen(),
        '/settings': (_) => const SettingsScreen(),
      },
      builder: (context, child) {
        // Never let the OS font-scaling setting break the layout: clamp it
        // to a sane range so tall accessibility settings cannot overflow
        // the composer or the status pill.
        final mq = MediaQuery.of(context);
        return MediaQuery(
          data: mq.copyWith(
            textScaler: mq.textScaler.clamp(
              minScaleFactor: 0.85,
              maxScaleFactor: 1.3,
            ),
          ),
          child: child ?? const SizedBox.shrink(),
        );
      },
    );
  }
}

class _RootGate extends StatelessWidget {
  const _RootGate();

  @override
  Widget build(BuildContext context) {
    final state = context.watch<AppState>();
    switch (state.status) {
      case AppStatus.loading:
      case AppStatus.authenticating:
        return const SplashView();
      case AppStatus.authenticated:
        return const HomeScreen();
      case AppStatus.unauthenticated:
      case AppStatus.error:
        return const AuthScreen();
    }
  }
}

/// Branded splash shown while the session is restored. The mark breathes
/// gently so a cold start feels alive rather than frozen.
class SplashView extends StatefulWidget {
  const SplashView({super.key});
  @override
  State<SplashView> createState() => _SplashViewState();
}

class _SplashViewState extends State<SplashView>
    with SingleTickerProviderStateMixin {
  late final AnimationController _c = AnimationController(
    vsync: this,
    duration: const Duration(milliseconds: 1400),
  )..repeat(reverse: true);

  @override
  void dispose() {
    _c.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    return Scaffold(
      backgroundColor: p.background,
      body: DecoratedBox(
        decoration: BoxDecoration(gradient: Palette.heroGlow),
        child: Center(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              ScaleTransition(
                scale: Tween(
                  begin: 0.96,
                  end: 1.04,
                ).animate(CurvedAnimation(parent: _c, curve: Curves.easeInOut)),
                child: const BrandMark(size: 84, radius: WebRadii.panel),
              ),
              const SizedBox(height: 22),
              Text(
                PowerXConfig.appName,
                style: TextStyle(
                  fontSize: 28,
                  fontWeight: FontWeight.w600,
                  letterSpacing: 1.4,
                  color: p.foreground,
                ),
              ),
              const SizedBox(height: 6),
              Text(
                PowerXConfig.tagline,
                style: TextStyle(fontSize: 13, color: p.mutedForeground),
              ),
              const SizedBox(height: 32),
              FadeTransition(
                opacity: Tween(begin: 0.3, end: 1.0).animate(_c),
                child: SizedBox(
                  width: 24,
                  height: 24,
                  child: CircularProgressIndicator(
                    strokeWidth: 2.2,
                    color: p.primary,
                  ),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
