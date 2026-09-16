import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:intl/intl.dart';
import 'package:provider/provider.dart';
import 'package:url_launcher/url_launcher.dart';

import '../config.dart';
import '../models.dart';
import '../services/supabase_auth.dart';
import '../state/app_state.dart';
import '../theme/palette.dart';

/// Native mirror of the WebUI's Profile & Billing + AI sections: account info,
/// credit balance, referral code, purchasable packages, payment verification,
/// and the active model.
class SettingsScreen extends StatefulWidget {
  const SettingsScreen({super.key});

  @override
  State<SettingsScreen> createState() => _SettingsScreenState();
}

class _SettingsScreenState extends State<SettingsScreen> {
  final _txRef = TextEditingController();
  final _txnId = TextEditingController();
  bool _verifying = false;
  VerifyPaymentResult? _verifyResult;
  bool? _referralUsed;
  bool _loadingReferral = false;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) {
      context.read<AppState>().refreshCredits();
      _loadReferralStatus();
    });
  }

  Future<void> _loadReferralStatus() async {
    setState(() => _loadingReferral = true);
    final used = await context.read<AppState>().referralUsedStatus();
    if (mounted) {
      setState(() {
        _referralUsed = used;
        _loadingReferral = false;
      });
    }
  }

  Future<void> _openPayment(String url) async {
    final uri = Uri.tryParse(url);
    if (uri == null) return;
    try {
      await launchUrl(uri, mode: LaunchMode.externalApplication);
    } catch (_) {}
  }

  Future<void> _verify() async {
    FocusScope.of(context).unfocus();
    if (_txRef.text.trim().isEmpty) {
      setState(
        () =>
            _verifyResult = const VerifyPaymentResult(
              ok: false,
              error: 'Enter your Flutterwave transaction reference.',
            ),
      );
      return;
    }
    setState(() {
      _verifying = true;
      _verifyResult = null;
    });
    final res = await context.read<AppState>().verifyPayment(
      _txRef.text,
      transactionId: _txnId.text,
    );
    if (mounted) {
      setState(() {
        _verifying = false;
        _verifyResult = res;
      });
      if (res.ok) {
        _txRef.clear();
        _txnId.clear();
      }
    }
  }

  @override
  void dispose() {
    _txRef.dispose();
    _txnId.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final state = context.watch<AppState>();
    final credits = state.credits;
    final packages = state.paymentPackages;
    final hasPayment = state.paymentUrl.isNotEmpty;

    return Scaffold(
      backgroundColor: Palette.bg0,
      appBar: AppBar(
        backgroundColor: Palette.bg0,
        elevation: 0,
        title: const Text(
          'Settings',
          style: TextStyle(fontWeight: FontWeight.w800),
        ),
        centerTitle: true,
      ),
      body: ListView(
        padding: const EdgeInsets.fromLTRB(16, 8, 16, 32),
        children: [
          _SectionTitle('Account'),
          _Card(
            children: [
              _Row(
                icon: Icons.badge_outlined,
                title: 'Email',
                value: state.email ?? 'Signed in',
              ),
              const _Divider(),
              _Row(
                icon: Icons.person_outline,
                title: 'Name',
                value:
                    state.displayName?.isNotEmpty == true
                        ? state.displayName!
                        : '—',
              ),
              const _Divider(),
              _Row(
                icon: Icons.fingerprint_rounded,
                title: 'Account ID',
                value:
                    state.supabaseUserId != null
                        ? '${state.supabaseUserId!.substring(0, 8)}…'
                        : '—',
              ),
            ],
          ),
          const SizedBox(height: 22),
          _SectionTitle('Credits'),
          _Card(
            children: [
              ListTile(
                contentPadding: EdgeInsets.zero,
                leading: const Icon(
                  Icons.monetization_on_outlined,
                  color: Palette.warning,
                ),
                title: const Text(
                  'Balance',
                  style: TextStyle(color: Palette.textSecondary, fontSize: 13),
                ),
                trailing:
                    state.creditsLoading && credits == null
                        ? const SizedBox(
                          width: 18,
                          height: 18,
                          child: CircularProgressIndicator(
                            strokeWidth: 2,
                            color: Palette.accent,
                          ),
                        )
                        : Text(
                          credits != null ? '${credits.total} credits' : '—',
                          style: const TextStyle(
                            color: Palette.textPrimary,
                            fontWeight: FontWeight.w800,
                            fontSize: 16,
                          ),
                        ),
              ),
              if (credits != null) ...[
                const _Divider(),
                Padding(
                  padding: const EdgeInsets.symmetric(vertical: 4),
                  child: Wrap(
                    spacing: 14,
                    runSpacing: 6,
                    children: [
                      _StatChip(label: 'Daily', value: '${credits.daily}'),
                      _StatChip(
                        label: 'Purchased',
                        value: '${credits.purchased}',
                      ),
                      _StatChip(label: 'Granted', value: '${credits.granted}'),
                      _StatChip(label: 'Drain', value: '${credits.drainRate}x'),
                    ],
                  ),
                ),
              ],
              const _Divider(),
              ListTile(
                contentPadding: EdgeInsets.zero,
                leading: const Icon(
                  Icons.refresh_rounded,
                  color: Palette.textTertiary,
                ),
                title: const Text(
                  'Refresh balance',
                  style: TextStyle(color: Palette.textPrimary),
                ),
                onTap: () => state.refreshCredits(),
              ),
            ],
          ),
          const SizedBox(height: 22),
          _SectionTitle('Referral'),
          _Card(
            children: [
              _Row(
                icon: Icons.card_giftcard,
                title: 'Your referral code',
                value: state.email ?? '—',
                copyValue: state.email,
              ),
              const _Divider(),
              _Row(
                icon: Icons.shield_outlined,
                title: 'Code status',
                value:
                    _loadingReferral
                        ? '…'
                        : (_referralUsed == true
                            ? 'Used'
                            : (_referralUsed == false
                                ? 'Available — not used yet'
                                : 'Unknown')),
              ),
              const Padding(
                padding: EdgeInsets.fromLTRB(16, 4, 16, 12),
                child: Text(
                  'Share your email as a referral code. A friend who signs up with it gets 700 bonus credits. Each code works once.',
                  style: TextStyle(color: Palette.textTertiary, fontSize: 12),
                ),
              ),
            ],
          ),
          if (packages.isNotEmpty) ...[
            const SizedBox(height: 22),
            _SectionTitle('Buy Credits'),
            _Card(
              children: [
                for (var i = 0; i < packages.length; i++) ...[
                  if (i > 0) const _Divider(),
                  _PackageRow(pkg: packages[i]),
                ],
                if (hasPayment) ...[
                  const _Divider(),
                  Padding(
                    padding: const EdgeInsets.fromLTRB(0, 12, 0, 4),
                    child: FilledButton.icon(
                      onPressed: () => _openPayment(state.paymentUrl),
                      icon: const Icon(Icons.credit_card_rounded, size: 18),
                      label: const Text('Buy credits'),
                      style: FilledButton.styleFrom(
                        backgroundColor: Palette.accent,
                        minimumSize: const Size.fromHeight(48),
                      ),
                    ),
                  ),
                ],
              ],
            ),
          ],
          const SizedBox(height: 22),
          _SectionTitle('Verify Payment'),
          _Card(
            children: [
              Padding(
                padding: const EdgeInsets.all(14),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    TextField(
                      controller: _txRef,
                      style: const TextStyle(color: Palette.textPrimary),
                      decoration: const InputDecoration(
                        labelText: 'Flutterwave transaction reference',
                        hintText: 'e.g. FLW-12345…',
                      ),
                    ),
                    const SizedBox(height: 12),
                    TextField(
                      controller: _txnId,
                      style: const TextStyle(color: Palette.textPrimary),
                      decoration: const InputDecoration(
                        labelText: 'Transaction ID (optional)',
                      ),
                    ),
                    const SizedBox(height: 14),
                    Row(
                      children: [
                        Expanded(
                          child: FilledButton.icon(
                            onPressed: _verifying ? null : _verify,
                            icon:
                                _verifying
                                    ? const SizedBox(
                                      width: 16,
                                      height: 16,
                                      child: CircularProgressIndicator(
                                        strokeWidth: 2,
                                        color: Palette.textPrimary,
                                      ),
                                    )
                                    : const Icon(
                                      Icons.verified_user_outlined,
                                      size: 18,
                                    ),
                            label: Text(
                              _verifying ? 'Verifying…' : 'Verify payment',
                            ),
                            style: FilledButton.styleFrom(
                              backgroundColor: Palette.accent,
                              minimumSize: const Size.fromHeight(46),
                            ),
                          ),
                        ),
                      ],
                    ),
                    if (_verifyResult != null)
                      Padding(
                        padding: const EdgeInsets.only(top: 10),
                        child: Text(
                          _verifyResult!.ok
                              ? 'Payment verified. ${_verifyResult!.credits ?? 0} credits added.'
                              : (_verifyResult!.error ??
                                  'Verification failed.'),
                          style: TextStyle(
                            color:
                                _verifyResult!.ok
                                    ? Palette.accentSoft
                                    : Palette.danger,
                            fontSize: 13,
                          ),
                        ),
                      ),
                  ],
                ),
              ),
            ],
          ),
          const SizedBox(height: 22),
          _SectionTitle('AI'),
          _Card(
            children: [
              _Row(
                icon: Icons.smart_toy_outlined,
                title: 'Current model',
                value: state.modelName ?? 'Not configured',
              ),
            ],
          ),
          const SizedBox(height: 22),
          _SectionTitle('About'),
          _Card(
            children: [
              _Row(
                icon: Icons.info_outline,
                title: 'App',
                value: '${PowerXConfig.appName} v${PowerXConfig.appVersion}',
              ),
              const _Divider(),
              _Row(
                icon: Icons.dns_outlined,
                title: 'Gateway',
                value: Uri.parse(PowerXConfig.origin).host,
              ),
            ],
          ),
          const SizedBox(height: 28),
          OutlinedButton.icon(
            onPressed: () async {
              await state.signOut();
              if (context.mounted) {
                Navigator.of(
                  context,
                ).pushNamedAndRemoveUntil('/auth', (r) => false);
              }
            },
            icon: const Icon(Icons.logout_rounded, color: Palette.danger),
            label: const Text(
              'Sign out',
              style: TextStyle(color: Palette.danger),
            ),
            style: OutlinedButton.styleFrom(
              side: const BorderSide(color: Color(0x66FF5252)),
            ),
          ),
        ],
      ),
    );
  }
}

class _SectionTitle extends StatelessWidget {
  const _SectionTitle(this.text);
  final String text;
  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(left: 4, bottom: 8),
      child: Text(
        text.toUpperCase(),
        style: const TextStyle(
          color: Palette.textTertiary,
          fontSize: 12,
          fontWeight: FontWeight.w700,
          letterSpacing: 0.6,
        ),
      ),
    );
  }
}

class _Card extends StatelessWidget {
  const _Card({required this.children});
  final List<Widget> children;
  @override
  Widget build(BuildContext context) {
    return Container(
      decoration: BoxDecoration(
        color: Palette.bg2,
        borderRadius: BorderRadius.circular(16),
        border: Border.all(color: Palette.borderSoft),
      ),
      clipBehavior: Clip.antiAlias,
      child: Column(children: children),
    );
  }
}

class _Divider extends StatelessWidget {
  const _Divider();
  @override
  Widget build(BuildContext context) =>
      Divider(color: Palette.borderSoft, height: 1, indent: 16, endIndent: 16);
}

class _Row extends StatelessWidget {
  const _Row({
    required this.icon,
    required this.title,
    required this.value,
    this.copyValue,
  });
  final IconData icon;
  final String title;
  final String value;
  final String? copyValue;

  @override
  Widget build(BuildContext context) {
    return ListTile(
      contentPadding: const EdgeInsets.symmetric(horizontal: 16, vertical: 2),
      leading: Icon(icon, color: Palette.textTertiary, size: 22),
      title: Text(
        title,
        style: const TextStyle(color: Palette.textSecondary, fontSize: 14),
      ),
      trailing: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 180),
            child: Text(
              value,
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              textAlign: TextAlign.right,
              style: const TextStyle(color: Palette.textPrimary, fontSize: 14),
            ),
          ),
          if (copyValue != null && copyValue!.isNotEmpty)
            IconButton(
              visualDensity: VisualDensity.compact,
              icon: const Icon(
                Icons.copy_rounded,
                size: 16,
                color: Palette.textTertiary,
              ),
              onPressed: () {
                Clipboard.setData(ClipboardData(text: copyValue!));
                ScaffoldMessenger.of(
                  context,
                ).showSnackBar(const SnackBar(content: Text('Copied')));
              },
            ),
        ],
      ),
    );
  }
}

class _StatChip extends StatelessWidget {
  const _StatChip({required this.label, required this.value});
  final String label;
  final String value;
  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
      decoration: BoxDecoration(
        color: Palette.scrim(0.25),
        borderRadius: BorderRadius.circular(8),
      ),
      child: Text(
        '$label: $value',
        style: const TextStyle(color: Palette.textSecondary, fontSize: 12.5),
      ),
    );
  }
}

class _PackageRow extends StatelessWidget {
  const _PackageRow({required this.pkg});
  final PaymentPackage pkg;
  @override
  Widget build(BuildContext context) {
    return ListTile(
      contentPadding: const EdgeInsets.symmetric(horizontal: 16, vertical: 2),
      title: Text(
        pkg.name,
        style: const TextStyle(color: Palette.textPrimary, fontSize: 14),
      ),
      subtitle: Text(
        '${NumberFormat.decimalPattern().format(pkg.credits)} credits',
        style: const TextStyle(color: Palette.textTertiary, fontSize: 12.5),
      ),
      trailing: Text(
        '\$${pkg.amountUsd.toStringAsFixed(2)}',
        style: const TextStyle(
          color: Palette.textPrimary,
          fontWeight: FontWeight.w700,
          fontSize: 15,
        ),
      ),
    );
  }
}
