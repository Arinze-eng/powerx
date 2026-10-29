import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import 'package:url_launcher/url_launcher.dart';

import '../config.dart';
import '../theme/palette.dart';
import '../state/app_state.dart';
import '../widgets/brand.dart';
import 'recovery_code_screen.dart' show kRecoveryAdminContact;

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
  final _code = TextEditingController();
  bool _isSignUp = false;
  bool _forgot = false;
  bool _obscure = true;

  @override
  void dispose() {
    _email.dispose();
    _password.dispose();
    _name.dispose();
    _referral.dispose();
    _code.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    FocusScope.of(context).unfocus();
    if (_forgot) {
      await _submitReset();
      return;
    }
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

  /// "Forgot password": email + recovery code + new password. On success the
  /// gate shows the new recovery code, which must be copied before sign-in.
  Future<void> _submitReset() async {
    final state = context.read<AppState>();
    final em = _email.text.trim();
    final code = _code.text.trim();
    final pw = _password.text;
    if (em.isEmpty || !em.contains('@')) {
      _toast('Enter a valid email');
      return;
    }
    if (code.isEmpty) {
      _toast('Enter your recovery code');
      return;
    }
    if (pw.length < 6) {
      _toast('New password must be at least 6 characters');
      return;
    }
    final error = await state.resetPasswordWithCode(em, code, pw);
    if (!mounted) return;
    if (error != null) {
      _toast(error);
      return;
    }
    // Success: RootGate now shows the new recovery code. Clear the form so
    // the old code and password are not left on screen.
    _code.clear();
    _password.clear();
    setState(() => _forgot = false);
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
        decoration:  BoxDecoration(gradient: Palette.heroGlow),
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
                     Center(
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
                        _forgot
                            ? 'Reset your password with your recovery code'
                            : _isSignUp
                            ? 'Create your account'
                            : 'Welcome back — sign in to continue',
                        style:  TextStyle(
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
                    if (_forgot) ...[
                      TextField(
                        controller: _code,
                        textCapitalization: TextCapitalization.characters,
                        autocorrect: false,
                        enableSuggestions: false,
                        decoration: InputDecoration(
                          labelText: 'Recovery code',
                          prefixIcon: Icon(
                            Icons.key_outlined,
                            color: Palette.textTertiary,
                          ),
                        ),
                      ),
                      const SizedBox(height: 14),
                    ],
                    TextField(
                      controller: _password,
                      obscureText: _obscure,
                      textInputAction: TextInputAction.done,
                      onSubmitted: (_) => _submit(),
                      decoration: InputDecoration(
                        labelText: _forgot ? 'New password' : 'Password',
                        prefixIcon:  Icon(
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
                       Padding(
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
                                _forgot
                                    ? 'Reset password'
                                    : _isSignUp
                                    ? 'Sign up'
                                    : 'Sign in',
                                style: const TextStyle(
                                  fontSize: 16,
                                  fontWeight: FontWeight.w700,
                                ),
                              ),
                    ),
                    if (!_isSignUp && !_forgot)
                      Align(
                        alignment: Alignment.centerRight,
                        child: TextButton(
                          onPressed: () => setState(() {
                            _forgot = true;
                            state.errorMessage = null;
                          }),
                          child: Text(
                            'Forgot password?',
                            style: TextStyle(
                              fontWeight: FontWeight.w600,
                              color: Palette.accentSoft,
                            ),
                          ),
                        ),
                      ),
                    if (_forgot)
                      TextButton(
                        onPressed: () => launchUrl(
                          Uri(scheme: 'mailto', path: kRecoveryAdminContact),
                          mode: LaunchMode.externalApplication,
                        ),
                        child: Text(
                          'Lost your recovery code? Contact admin at $kRecoveryAdminContact',
                          textAlign: TextAlign.center,
                          style: TextStyle(
                            fontSize: 12,
                            color: Palette.textTertiary,
                          ),
                        ),
                      ),
                    if (_forgot)
                      TextButton(
                        onPressed: () => setState(() => _forgot = false),
                        child: Text(
                          'Back to sign in',
                          style: TextStyle(
                            fontWeight: FontWeight.w700,
                            color: Palette.accentSoft,
                          ),
                        ),
                      )
                    else ...[
                    const SizedBox(height: 14),
                    Row(
                      mainAxisAlignment: MainAxisAlignment.center,
                      children: [
                        Text(
                          _isSignUp
                              ? 'Already have an account?'
                              : 'New to ${PowerXConfig.appName}?',
                          style:  TextStyle(
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
                            style:  TextStyle(
                              fontWeight: FontWeight.w700,
                              color: Palette.accentSoft,
                            ),
                          ),
                        ),
                      ],
                    ),
                    ],
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
