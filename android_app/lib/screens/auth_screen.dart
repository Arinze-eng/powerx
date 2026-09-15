import 'package:flutter/material.dart';

import '../config.dart';
import '../state/app_state.dart';
import 'package:provider/provider.dart';

class AuthScreen extends StatefulWidget {
  const AuthScreen({super.key});

  @override
  State<AuthScreen> createState() => _AuthScreenState();
}

class _AuthScreenState extends State<AuthScreen> {
  final _email = TextEditingController();
  final _password = TextEditingController();
  final _name = TextEditingController();
  bool _isSignUp = false;
  bool _obscure = true;

  @override
  void dispose() {
    _email.dispose();
    _password.dispose();
    _name.dispose();
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
      final ok = await state.signUp(em, pw, _name.text);
      if (ok && mounted) Navigator.of(context).pushReplacementNamed('/home');
    } else {
      await state.signIn(em, pw);
    }
  }

  void _toast(String msg) {
    ScaffoldMessenger.of(context)
        .showSnackBar(SnackBar(content: Text(msg)));
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
      backgroundColor: const Color(0xFF0B1020),
      body: SafeArea(
        child: Center(
          child: SingleChildScrollView(
            padding: const EdgeInsets.symmetric(horizontal: 28, vertical: 32),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                const SizedBox(height: 24),
                Center(
                  child: Container(
                    width: 76,
                    height: 76,
                    decoration: BoxDecoration(
                      gradient: const LinearGradient(
                        colors: [Color(0xFF2E7D32), Color(0xFF66BB6A)],
                        begin: Alignment.topLeft,
                        end: Alignment.bottomRight,
                      ),
                      borderRadius: BorderRadius.circular(20),
                    ),
                    child: const Center(
                        child: Text('⚡', style: TextStyle(fontSize: 38))),
                  ),
                ),
                const SizedBox(height: 18),
                const Center(
                  child: Text(PowerXConfig.appName,
                      style: TextStyle(
                          fontSize: 28,
                          fontWeight: FontWeight.w800,
                          color: Colors.white)),
                ),
                const SizedBox(height: 4),
                Center(
                  child: Text(
                    _isSignUp ? 'Create your account' : 'Welcome back',
                    style: const TextStyle(color: Colors.white54),
                  ),
                ),
                const SizedBox(height: 32),
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
                  style: const TextStyle(color: Colors.white),
                  decoration: InputDecoration(
                    labelText: 'Password',
                    prefixIcon: const Icon(Icons.lock_outline,
                        color: Colors.white54),
                    suffixIcon: IconButton(
                      icon: Icon(
                          _obscure ? Icons.visibility_off : Icons.visibility,
                          color: Colors.white54),
                      onPressed: () => setState(() => _obscure = !_obscure),
                    ),
                  ),
                ),
                const SizedBox(height: 26),
                FilledButton(
                  onPressed: busy ? null : _submit,
                  style: FilledButton.styleFrom(
                    minimumSize: const Size.fromHeight(52),
                    backgroundColor: const Color(0xFF2E7D32),
                    shape: RoundedRectangleBorder(
                        borderRadius: BorderRadius.circular(14)),
                  ),
                  child: busy
                      ? const SizedBox(
                          width: 22,
                          height: 22,
                          child: CircularProgressIndicator(
                              strokeWidth: 2.4, color: Colors.white))
                      : Text(_isSignUp ? 'Sign up' : 'Sign in',
                          style: const TextStyle(
                              fontSize: 16, fontWeight: FontWeight.w700)),
                ),
                const SizedBox(height: 18),
                Row(
                  mainAxisAlignment: MainAxisAlignment.center,
                  children: [
                    Text(
                      _isSignUp ? 'Already have an account?' : "New to PowerX?",
                      style: const TextStyle(color: Colors.white54),
                    ),
                    TextButton(
                      onPressed: () => setState(() {
                        _isSignUp = !_isSignUp;
                        state.errorMessage = null;
                      }),
                      child: Text(_isSignUp ? 'Sign in' : 'Create one',
                          style: const TextStyle(fontWeight: FontWeight.w700)),
                    ),
                  ],
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }

  Widget _field(TextEditingController c, String label, IconData icon, bool obscure) {
    return TextField(
      controller: c,
      obscureText: obscure,
      keyboardType: TextInputType.emailAddress,
      style: const TextStyle(color: Colors.white),
      decoration: InputDecoration(
        labelText: label,
        prefixIcon: Icon(icon, color: Colors.white54),
      ),
    );
  }
}
