import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:provider/provider.dart';

import 'config.dart';
import 'screens/auth_screen.dart';
import 'screens/home_screen.dart';
import 'screens/settings_screen.dart';
import 'state/app_state.dart';
import 'theme/app_theme.dart';
import 'theme/palette.dart';
import 'widgets/brand.dart';

void main() {
  WidgetsFlutterBinding.ensureInitialized();
  SystemChrome.setSystemUIOverlayStyle(
    const SystemUiOverlayStyle(
      statusBarColor: Colors.transparent,
      systemNavigationBarColor: Palette.bg0,
      statusBarIconBrightness: Brightness.light,
      systemNavigationBarIconBrightness: Brightness.light,
    ),
  );
  runApp(const PowerXApp());
}

class PowerXApp extends StatelessWidget {
  const PowerXApp({super.key});

  @override
  Widget build(BuildContext context) {
    return ChangeNotifierProvider(
      create: (_) => AppState()..init(),
      child: MaterialApp(
        title: PowerXConfig.appName,
        debugShowCheckedModeBanner: false,
        theme: AppTheme.build(),
        home: const _RootGate(),
        routes: {
          '/auth': (_) => const AuthScreen(),
          '/home': (_) => const HomeScreen(),
          '/settings': (_) => const SettingsScreen(),
        },
      ),
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
    return Scaffold(
      backgroundColor: Palette.bg0,
      body: DecoratedBox(
        decoration: const BoxDecoration(gradient: Palette.heroGlow),
        child: Center(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              ScaleTransition(
                scale: Tween(
                  begin: 0.96,
                  end: 1.04,
                ).animate(CurvedAnimation(parent: _c, curve: Curves.easeInOut)),
                child: const BrandMark(size: 92),
              ),
              const SizedBox(height: 24),
              const Text(
                PowerXConfig.appName,
                style: TextStyle(
                  fontSize: 26,
                  fontWeight: FontWeight.w800,
                  letterSpacing: 1.2,
                  color: Palette.textPrimary,
                ),
              ),
              const SizedBox(height: 8),
              const Text(
                PowerXConfig.tagline,
                style: TextStyle(fontSize: 13, color: Palette.textTertiary),
              ),
              const SizedBox(height: 34),
              FadeTransition(
                opacity: Tween(begin: 0.3, end: 1.0).animate(_c),
                child: const SizedBox(
                  width: 26,
                  height: 26,
                  child: CircularProgressIndicator(
                    strokeWidth: 2.4,
                    color: Palette.accent,
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
