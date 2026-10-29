import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../theme/palette.dart';

/// Contact for lost recovery codes. Shown on every recovery-code screen.
const String kRecoveryAdminContact = 'mathsscience399@gmail.com';

/// Shows a recovery code once and requires the user to copy it before
/// [onContinue] is enabled.
///
/// [code] null with [error] null means the code is still being created.
/// [error] set means creation failed: show it with a retry action.
class RecoveryCodeScreen extends StatefulWidget {
  const RecoveryCodeScreen({
    super.key,
    required this.code,
    required this.error,
    required this.onContinue,
    this.continueLabel = 'I have copied it, continue',
    this.onRetry,
    this.onSignOut,
  });

  final String? code;
  final String? error;
  final VoidCallback onContinue;
  final String continueLabel;
  final VoidCallback? onRetry;
  final VoidCallback? onSignOut;

  @override
  State<RecoveryCodeScreen> createState() => _RecoveryCodeScreenState();
}

class _RecoveryCodeScreenState extends State<RecoveryCodeScreen> {
  bool _copied = false;

  Future<void> _copy() async {
    final code = widget.code;
    if (code == null) return;
    await Clipboard.setData(ClipboardData(text: code));
    if (!mounted) return;
    setState(() => _copied = true);
    ScaffoldMessenger.of(context).showSnackBar(
      const SnackBar(content: Text('Recovery code copied')),
    );
  }

  @override
  Widget build(BuildContext context) {
    final code = widget.code;
    final error = widget.error;
    return Scaffold(
      backgroundColor: Palette.bg0,
      body: DecoratedBox(
        decoration: BoxDecoration(gradient: Palette.heroGlow),
        child: SafeArea(
          child: Center(
            child: SingleChildScrollView(
              padding: const EdgeInsets.symmetric(horizontal: 26, vertical: 32),
              child: ConstrainedBox(
                constraints: const BoxConstraints(maxWidth: 440),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    Text(
                      'Save your recovery code',
                      textAlign: TextAlign.center,
                      style: TextStyle(
                        fontSize: 22,
                        fontWeight: FontWeight.w800,
                        color: Palette.textPrimary,
                      ),
                    ),
                    const SizedBox(height: 10),
                    Text(
                      'This code is the only way to reset your password. '
                      'Copy it and keep it somewhere safe. Each code works once: '
                      'after you use it to reset, you will get a new one.',
                      textAlign: TextAlign.center,
                      style: TextStyle(
                        color: Palette.textSecondary,
                        fontSize: 13.5,
                        height: 1.4,
                      ),
                    ),
                    const SizedBox(height: 18),
                    _warningBox(),
                    const SizedBox(height: 22),
                    if (code != null)
                      _codeBox(code)
                    else if (error == null)
                      const Center(
                        child: Padding(
                          padding: EdgeInsets.all(24),
                          child: CircularProgressIndicator(),
                        ),
                      ),
                    if (error != null) ...[
                      Text(
                        error,
                        textAlign: TextAlign.center,
                        style: TextStyle(color: Palette.danger, fontSize: 13.5),
                      ),
                      const SizedBox(height: 14),
                      if (widget.onRetry != null)
                        OutlinedButton(
                          onPressed: widget.onRetry,
                          child: const Text('Try again'),
                        ),
                      if (widget.onSignOut != null) ...[
                        const SizedBox(height: 8),
                        TextButton(
                          onPressed: widget.onSignOut,
                          child: const Text('Sign out'),
                        ),
                      ],
                    ],
                    if (code != null) ...[
                      const SizedBox(height: 14),
                      OutlinedButton.icon(
                        onPressed: _copy,
                        icon: const Icon(Icons.copy_rounded),
                        label: Text(_copied ? 'Copied' : 'Copy code'),
                      ),
                      const SizedBox(height: 22),
                      FilledButton(
                        onPressed: _copied ? widget.onContinue : null,
                        style: FilledButton.styleFrom(
                          minimumSize: const Size.fromHeight(52),
                          shape: RoundedRectangleBorder(
                            borderRadius: BorderRadius.circular(14),
                          ),
                        ),
                        child: Text(
                          _copied
                              ? widget.continueLabel
                              : 'Copy the code to continue',
                          style: const TextStyle(
                            fontSize: 15,
                            fontWeight: FontWeight.w700,
                          ),
                        ),
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

  Widget _warningBox() {
    return Container(
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: Palette.warning.withValues(alpha: 0.12),
        borderRadius: BorderRadius.circular(14),
        border: Border.all(color: Palette.warning.withValues(alpha: 0.4)),
      ),
      child: Text(
        'Lost recovery code means lost account. If you lose it, contact admin '
        '$kRecoveryAdminContact.',
        textAlign: TextAlign.center,
        style: TextStyle(
          color: Palette.textPrimary,
          fontSize: 13,
          fontWeight: FontWeight.w600,
          height: 1.4,
        ),
      ),
    );
  }

  Widget _codeBox(String code) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 18),
      decoration: BoxDecoration(
        color: Palette.bg2,
        borderRadius: BorderRadius.circular(14),
        border: Border.all(color: Palette.border),
      ),
      child: SelectableText(
        code,
        key: const ValueKey('recovery-code-text'),
        textAlign: TextAlign.center,
        style: TextStyle(
          fontFamily: 'monospace',
          fontSize: 20,
          fontWeight: FontWeight.w800,
          letterSpacing: 1.5,
          color: Palette.textPrimary,
        ),
      ),
    );
  }
}
