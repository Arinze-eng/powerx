import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../config.dart';
import '../theme/palette.dart';
import '../state/app_state.dart';
import '../widgets/brand.dart';

class AuthScreen extends StatefulWidget {
  const AuthScreen({super.key});

  @override
  State<AuthScreen> createState() => _AuthScreenState();
}

class _AuthScreenState extends State<AuthScreen> {
  final _email = TextEditingController();
  final _password = TextEditingController();
  final _name = TextEditingController();
  final _referral = TextEditingController();
  bool _isSignUp = false;
  bool _obscure = true;

  @override
  void dispose() {
    _email.dispose();
    _password.dispose();
    _name.dispose();
    _referral.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    FocusScope.of(context).unfocus();
    final state = context.read<AppState>();
    final em = _email.text.trim();
    final pw = _password.text;
    if (em.isEmpty || !em.contains('@')) {
      _toast('Enter a valid email');
      return;
    }
    if (pw.length < 6) {
      _toast('Password must be at least 6 characters');
      return;
    }
    if (_isSignUp) {
      await state.signUp(em, pw, _name.text, referral: _referral.text.trim());
    } else {
      await state.signIn(em, pw);
    }
    // RootGate swaps Home/Auth by status; but when this screen is the pushed
    // '/auth' route (after sign-out from settings) it must be dismissed too,
    // otherwise the previous account's screens could linger on the stack.
    if (mounted && state.status == AppStatus.authenticated) {
      final nav = Navigator.of(context);
      if (nav.canPop()) {
        nav.popUntil((route) => route.isFirst);
      }
    }
  }

  void _toast(String msg) {
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(msg)));
  }

  @override
  Widget build(BuildContext context) {
    final state = context.watch<AppState>();
    final busy = state.status == AppStatus.authenticating;

    // Surface auth errors as a snackbar once.
    if (state.status == AppStatus.error && state.errorMessage != null) {
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (mounted) _toast(state.errorMessage!);
      });
    }

    return Scaffold(
      backgroundColor: Palette.bg0,
      body: DecoratedBox(
        decoration: const BoxDecoration(gradient: Palette.heroGlow),
        child: SafeArea(
          child: Center(
            child: SingleChildScrollView(
              padding: const EdgeInsets.symmetric(horizontal: 26, vertical: 32),
              child: ConstrainedBox(
                constraints: const BoxConstraints(maxWidth: 440),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    const SizedBox(height: 12),
                    const Center(child: BrandMark(size: 76)),
                    const SizedBox(height: 20),
                    const Center(
                      child: Text(
                        PowerXConfig.appName,
                        style: TextStyle(
                          fontSize: 27,
                          fontWeight: FontWeight.w800,
                          letterSpacing: 1.0,
                          color: Palette.textPrimary,
                        ),
                      ),
                    ),
                    const SizedBox(height: 6),
                    Center(
                      child: Text(
                        _isSignUp
                            ? 'Create your account'
                            : 'Welcome back — sign in to continue',
                        style: const TextStyle(
                          color: Palette.textTertiary,
                          fontSize: 13.5,
                        ),
                      ),
                    ),
                    const SizedBox(height: 30),
                    if (_isSignUp) ...[
                      _field(_name, 'Full name', Icons.person_outline, false),
                      const SizedBox(height: 14),
                    ],
                    _field(_email, 'Email', Icons.alternate_email, false),
                    const SizedBox(height: 14),
                    TextField(
                      controller: _password,
                      obscureText: _obscure,
                      textInputAction: TextInputAction.done,
                      onSubmitted: (_) => _submit(),
                      decoration: InputDecoration(
                        labelText: 'Password',
                        prefixIcon: const Icon(
                          Icons.lock_outline,
                          color: Palette.textTertiary,
                        ),
                        suffixIcon: IconButton(
                          icon: Icon(
                            _obscure ? Icons.visibility_off : Icons.visibility,
                            color: Palette.textTertiary,
                          ),
                          onPressed: () => setState(() => _obscure = !_obscure),
                        ),
                      ),
                    ),
                    if (_isSignUp) ...[
                      const SizedBox(height: 14),
                      _field(
                        _referral,
                        'Referral code (optional)',
                        Icons.card_giftcard,
                        false,
                      ),
                      const SizedBox(height: 8),
                      const Padding(
                        padding: EdgeInsets.only(left: 4),
                        child: Text(
                          "Enter your friend's email as a referral code and get "
                          '700 bonus credits. Each code works once.',
                          style: TextStyle(
                            color: Palette.textTertiary,
                            fontSize: 12,
                          ),
                        ),
                      ),
                    ],
                    const SizedBox(height: 26),
                    FilledButton(
                      onPressed: busy ? null : _submit,
                      style: FilledButton.styleFrom(
                        minimumSize: const Size.fromHeight(52),
                        shape: RoundedRectangleBorder(
                          borderRadius: BorderRadius.circular(14),
                        ),
                      ),
                      child:
                          busy
                              ? const SizedBox(
                                width: 22,
                                height: 22,
                                child: CircularProgressIndicator(
                                  strokeWidth: 2.4,
                                  color: Colors.white,
                                ),
                              )
                              : Text(
                                _isSignUp ? 'Sign up' : 'Sign in',
                                style: const TextStyle(
                                  fontSize: 16,
                                  fontWeight: FontWeight.w700,
                                ),
                              ),
                    ),
                    const SizedBox(height: 14),
                    Row(
                      mainAxisAlignment: MainAxisAlignment.center,
                      children: [
                        Text(
                          _isSignUp
                              ? 'Already have an account?'
                              : 'New to ${PowerXConfig.appName}?',
                          style: const TextStyle(
                            color: Palette.textTertiary,
                            fontSize: 13.5,
                          ),
                        ),
                        TextButton(
                          onPressed:
                              () => setState(() {
                                _isSignUp = !_isSignUp;
                                state.errorMessage = null;
                              }),
                          child: Text(
                            _isSignUp ? 'Sign in' : 'Create one',
                            style: const TextStyle(
                              fontWeight: FontWeight.w700,
                              color: Palette.accentSoft,
                            ),
                          ),
                        ),
                      ],
                    ),
                  ],
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }

  Widget _field(
    TextEditingController c,
    String label,
    IconData icon,
    bool obscure,
  ) {
    return TextField(
      controller: c,
      obscureText: obscure,
      keyboardType: TextInputType.emailAddress,
      decoration: InputDecoration(
        labelText: label,
        prefixIcon: Icon(icon, color: Palette.textTertiary),
      ),
    );
  }
}
