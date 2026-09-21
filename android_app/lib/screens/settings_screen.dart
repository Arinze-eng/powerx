import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:intl/intl.dart';
import 'package:provider/provider.dart';
import 'package:url_launcher/url_launcher.dart';

import '../config.dart';
import '../services/settings_api.dart';
import '../services/supabase_auth.dart';
import '../state/app_state.dart';
import '../state/theme_controller.dart';
import '../theme/palette.dart';
import '../theme/tokens.dart';
import '../widgets/brand.dart';
import '../widgets/settings_controls.dart';
import 'settings_sections.dart';

/// Native settings surface, laid out the way the WebUI lays it out.
///
/// The web renders the settings nav as a sidebar on desktop and as a dropdown
/// once the viewport is narrow (`SettingsSidebar`). On a phone we use the
/// dropdown form: the app bar title is the current section and the menu icon
/// opens the section list. Section order, labels, the grouped surfaces, the
/// 62px rows and the `#2997FF` switches all come from the same components the
/// browser uses, so the two clients read as one product.
class SettingsScreen extends StatefulWidget {
  const SettingsScreen({super.key, this.initialSection = SettingsSection.overview});

  final SettingsSection initialSection;

  @override
  State<SettingsScreen> createState() => _SettingsScreenState();
}

class _SettingsScreenState extends State<SettingsScreen> {
  late SettingsSection _section = widget.initialSection;

  final _txRef = TextEditingController();
  final _txnId = TextEditingController();
  bool _verifying = false;
  VerifyPaymentResult? _verifyResult;
  bool? _referralUsed;
  bool _loadingReferral = false;

  /// Section ids with a mutation in flight, so a switch shows a spinner and
  /// cannot be double-tapped.
  final Set<String> _busy = {};

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) {
      final state = context.read<AppState>();
      state.refreshCredits();
      state.loadSettings();
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
      setState(() {
        _verifyResult = const VerifyPaymentResult(
          ok: false,
          error: 'Enter your Flutterwave transaction reference.',
        );
      });
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
    if (!mounted) return;
    setState(() {
      _verifying = false;
      _verifyResult = res;
    });
    if (res.ok) {
      _txRef.clear();
      _txnId.clear();
    }
  }

  @override
  void dispose() {
    _txRef.dispose();
    _txnId.dispose();
    super.dispose();
  }

  // ---- Mutation helper ---------------------------------------------------

  /// Run a settings write over the socket, then refresh the settings document.
  ///
  /// Every write goes through the socket because that is the only channel the
  /// gateway accepts; see `SettingsApi`'s doc comment. Failures surface as a
  /// snack bar rather than a silent no-op, which is what made the old build
  /// feel broken.
  Future<void> _run(
    String key,
    Future<void> Function(WebUiMutation mutate) action,
  ) async {
    final state = context.read<AppState>();
    setState(() => _busy.add(key));
    try {
      await action(state.mutate);
      await state.refreshSettings();
      if (_section == SettingsSection.apps ||
          _section == SettingsSection.automations ||
          _section == SettingsSection.skills ||
          _section == SettingsSection.advanced) {
        await state.loadSettings();
      }
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('Could not save: $e')),
        );
      }
    } finally {
      if (mounted) setState(() => _busy.remove(key));
    }
  }

  bool _isBusy(String key) => _busy.contains(key);

  // ---- Build -------------------------------------------------------------

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    return Scaffold(
      backgroundColor: p.background,
      appBar: AppBar(
        backgroundColor: p.background,
        elevation: 0,
        scrolledUnderElevation: 0,
        leading: IconButton(
          icon: const Icon(Icons.arrow_back_rounded, size: 20),
          tooltip: 'Back to chat',
          onPressed: () => Navigator.of(context).maybePop(),
        ),
        titleSpacing: 0,
        title: Text(
          _section.label,
          style: TextStyle(
            fontSize: WebType.pageTitle,
            fontWeight: FontWeight.w600,
            letterSpacing: -0.3,
            color: p.foreground,
          ),
        ),
        actions: [
          IconButton(
            icon: const Icon(Icons.tune_rounded, size: 20),
            tooltip: 'All settings',
            onPressed: () => _pickSection(context),
          ),
          const SizedBox(width: 4),
        ],
      ),
      body: SafeArea(
        top: false,
        child: RefreshIndicator(
          color: p.primary,
          backgroundColor: p.card,
          onRefresh: () => context.read<AppState>().loadSettings(),
          child: ListView(
            padding: const EdgeInsets.fromLTRB(16, 8, 16, 40),
            children: [
              if (_section != SettingsSection.overview) const _BackToChatPill(),
              ..._sectionChildren(context),
              const SizedBox(height: 28),
              Center(
                child: BrandWordmark(fontSize: 13),
              ),
              const SizedBox(height: 18),
            ],
          ),
        ),
      ),
    );
  }

  List<Widget> _sectionChildren(BuildContext context) {
    switch (_section) {
      case SettingsSection.overview:
        return _overview(context);
      case SettingsSection.appearance:
        return _appearance(context);
      case SettingsSection.models:
        return _models(context);
      case SettingsSection.image:
        return _image(context);
      case SettingsSection.voice:
        return _voice(context);
      case SettingsSection.browser:
        return _browser(context);
      case SettingsSection.channels:
        return _channels(context);
      case SettingsSection.apps:
        return _apps(context);
      case SettingsSection.automations:
        return _automations(context);
      case SettingsSection.skills:
        return _skills(context);
      case SettingsSection.advanced:
        return _advanced(context);
    }
  }

  Future<void> _pickSection(BuildContext context) async {
    final p = context.palette;
    final chosen = await showModalBottomSheet<SettingsSection>(
      context: context,
      backgroundColor: p.popover,
      builder: (context) => SafeArea(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            const SizedBox(height: 8),
            Container(
              width: 36,
              height: 4,
              decoration: BoxDecoration(
                color: p.border,
                borderRadius: BorderRadius.circular(2),
              ),
            ),
            const SizedBox(height: 8),
            for (final s in SettingsSection.values)
              ListTile(
                dense: true,
                title: Text(
                  s.label,
                  style: TextStyle(
                    fontSize: WebType.settingsNav,
                    fontWeight: s == _section ? FontWeight.w600 : FontWeight.w500,
                    color: s == _section ? p.foreground : p.foreground.withValues(alpha: 0.85),
                  ),
                ),
                trailing: s == _section
                    ? Icon(Icons.check_rounded, size: 18, color: p.primary)
                    : null,
                onTap: () => Navigator.of(context).pop(s),
              ),
            const SizedBox(height: 8),
          ],
        ),
      ),
    );
    if (chosen != null && mounted) setState(() => _section = chosen);
  }

  // ---- Sections ---------------------------------------------------------

  List<Widget> _overview(BuildContext context) {
    final state = context.watch<AppState>();
    final p = context.palette;
    final credits = state.credits;
    final packages = state.paymentPackages;
    final settings = state.settings;

    return [
      SettingsSectionTitle('Account'),
      SettingsGroup(
        children: [
          ReadOnlyRow(title: 'Email', value: state.email ?? 'Signed in'),
          ReadOnlyRow(
            title: 'Name',
            value: (state.displayName?.isNotEmpty ?? false)
                ? state.displayName!
                : '—',
          ),
          ReadOnlyRow(
            title: 'Account ID',
            value: state.supabaseUserId != null
                ? '${state.supabaseUserId!.substring(0, 8)}…'
                : '—',
            description: 'The same account signs you in on the web app.',
          ),
        ],
      ),
      const SizedBox(height: 22),
      SettingsSectionTitle('Usage'),
      SettingsGroup(
        children: [
          ReadOnlyRow(
            title: 'Tokens, last 30 days',
            value: _tokens(settings?.usage.totalTokens30d ?? 0),
          ),
          ReadOnlyRow(
            title: 'Requests, last 30 days',
            value: '${settings?.usage.requests30d ?? 0}',
          ),
          ReadOnlyRow(
            title: 'Streak',
            value: settings == null
                ? '—'
                : '${settings.usage.currentStreakDays} d',
            description: settings == null
                ? null
                : 'Longest ${settings.usage.longestStreakDays} days, '
                    '${settings.usage.activeDays30d} active days in the last 30.',
          ),
        ],
      ),
      const SizedBox(height: 22),
      SettingsSectionTitle('Credits'),
      SettingsGroup(
        children: [
          SettingsRow(
            title: 'Balance',
            description: credits != null
                ? 'Daily ${credits.daily} · Purchased ${credits.purchased} · '
                    'Granted ${credits.granted} · Drain ${credits.drainRate}x'
                : null,
            child: state.creditsLoading && credits == null
                ? SizedBox(
                    width: 16,
                    height: 16,
                    child: CircularProgressIndicator(
                      strokeWidth: 2,
                      color: p.primary,
                    ),
                  )
                : Text(
                    credits != null ? '${credits.total}' : '—',
                    style: TextStyle(
                      fontSize: 15,
                      fontWeight: FontWeight.w600,
                      color: p.foreground,
                    ),
                  ),
          ),
          SettingsRow(
            title: 'Refresh balance',
            onTap: () => state.refreshCredits(),
          ),
        ],
      ),
      const SizedBox(height: 22),
      SettingsSectionTitle('Referral'),
      SettingsGroup(
        children: [
          SettingsRow(
            title: 'Your referral code',
            description:
                'A friend who signs up with it gets 700 bonus credits. Each code works once.',
            child: Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                Text(
                  state.email ?? '—',
                  style: TextStyle(fontSize: 13, color: p.mutedForeground),
                ),
                if (state.email != null)
                  IconButton(
                    visualDensity: VisualDensity.compact,
                    icon: Icon(
                      Icons.copy_rounded,
                      size: 16,
                      color: p.mutedForeground,
                    ),
                    tooltip: 'Copy',
                    onPressed: () {
                      Clipboard.setData(ClipboardData(text: state.email!));
                      ScaffoldMessenger.of(context).showSnackBar(
                        const SnackBar(content: Text('Referral code copied')),
                      );
                    },
                  ),
              ],
            ),
          ),
          ReadOnlyRow(
            title: 'Code status',
            value: _loadingReferral
                ? '…'
                : _referralUsed == true
                    ? 'Used'
                    : _referralUsed == false
                        ? 'Available'
                        : 'Unknown',
          ),
        ],
      ),
      if (packages.isNotEmpty) ...[
        const SizedBox(height: 22),
        SettingsSectionTitle('Buy credits'),
        SettingsGroup(
          children: [
            for (final pkg in packages)
              SettingsRow(
                title: pkg.name,
                description:
                    '${NumberFormat.decimalPattern().format(pkg.credits)} credits',
                child: Text(
                  '\$${pkg.amountUsd.toStringAsFixed(2)}',
                  style: TextStyle(
                    fontSize: 15,
                    fontWeight: FontWeight.w600,
                    color: p.foreground,
                  ),
                ),
              ),
            if (state.paymentUrl.isNotEmpty)
              Padding(
                padding: const EdgeInsets.all(16),
                child: SizedBox(
                  width: double.infinity,
                  child: FilledButton(
                    onPressed: () => _openPayment(state.paymentUrl),
                    child: const Text('Buy credits'),
                  ),
                ),
              ),
          ],
        ),
      ],
      const SizedBox(height: 22),
      SettingsSectionTitle('Verify payment'),
      SettingsGroup(
        children: [
          Padding(
            padding: const EdgeInsets.all(16),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                TextField(
                  controller: _txRef,
                  decoration: const InputDecoration(
                    labelText: 'Flutterwave transaction reference',
                    hintText: 'e.g. FLW-12345…',
                  ),
                ),
                const SizedBox(height: 12),
                TextField(
                  controller: _txnId,
                  decoration: const InputDecoration(
                    labelText: 'Transaction ID (optional)',
                  ),
                ),
                const SizedBox(height: 14),
                FilledButton(
                  onPressed: _verifying ? null : _verify,
                  child: Text(_verifying ? 'Verifying…' : 'Verify payment'),
                ),
                if (_verifyResult != null)
                  Padding(
                    padding: const EdgeInsets.only(top: 10),
                    child: Text(
                      _verifyResult!.ok
                          ? 'Payment verified. '
                              '${_verifyResult!.credits ?? 0} credits added.'
                          : (_verifyResult!.error ?? 'Verification failed.'),
                      style: TextStyle(
                        fontSize: 13,
                        color: _verifyResult!.ok ? p.success : p.destructive,
                      ),
                    ),
                  ),
              ],
            ),
          ),
        ],
      ),
      const SizedBox(height: 22),
      SettingsSectionTitle('About'),
      SettingsGroup(
        children: [
          ReadOnlyRow(
            title: 'App',
            value: '${PowerXConfig.appName} ${PowerXConfig.appVersion}',
          ),
          ReadOnlyRow(
            title: 'Server',
            value: state.appVersion == null
                ? (settings?.version.isNotEmpty == true
                    ? settings!.version
                    : '—')
                : state.appVersion!.version,
            description: state.appVersion?.gitSha.isNotEmpty == true
                ? 'build ${state.appVersion!.gitSha.substring(0, 7)}'
                : null,
          ),
          ReadOnlyRow(
            title: 'Gateway',
            value: Uri.parse(PowerXConfig.origin).host,
          ),
        ],
      ),
      const SizedBox(height: 22),
      const _SignOutButton(),
    ];
  }

  List<Widget> _appearance(BuildContext context) {
    final theme = context.watch<ThemeController>();
    final p = context.palette;
    return [
      SettingsSectionTitle('Theme'),
      SettingsGroup(
        children: [
          SettingsRow(
            title: 'Colour scheme',
            description: 'Follow the phone, or pin light or dark.',
            child: WebSegmentedControl<ThemeMode>(
              value: theme.mode,
              options: const [
                WebSegment(ThemeMode.system, 'System'),
                WebSegment(ThemeMode.light, 'Light'),
                WebSegment(ThemeMode.dark, 'Dark'),
              ],
              onChanged: (m) => theme.setMode(m),
            ),
          ),
          ReadOnlyRow(
            title: 'Currently showing',
            value: theme.isDark ? 'Dark' : 'Light',
            description: theme.mode == ThemeMode.system
                ? 'Following your phone setting.'
                : 'Pinned by you.',
          ),
        ],
      ),
      const SizedBox(height: 22),
      SettingsSectionTitle('Palette'),
      SettingsGroup(
        children: [
          _SwatchRow(
            title: 'Canvas',
            description: 'Page background',
            swatches: [p.background, p.isDark ? WebPalette.light.background : WebPalette.dark.background],
          ),
          _SwatchRow(
            title: 'Primary action',
            description: 'Filled buttons and strong emphasis',
            swatches: [p.primary],
          ),
          _SwatchRow(
            title: 'Highlight',
            description: 'The one accent — temporary chats, inline tokens',
            swatches: [p.highlight],
          ),
          _SwatchRow(
            title: 'Switch on',
            description: 'The single non-monochrome control',
            swatches: [p.toggleOn],
          ),
        ],
      ),
    ];
  }

  List<Widget> _models(BuildContext context) {
    final state = context.watch<AppState>();
    final agent = state.settings?.agent;
    final order = state.settings?.modelCallOrder ?? const [];
    return [
      if (state.settings?.requiresRestart == true) ...[
        RestartRequiredNotice(
          message: 'Some changes need the server to restart before they apply.',
        ),
        const SizedBox(height: 18),
      ],
      SettingsSectionTitle('Agent'),
      SettingsGroup(
        children: [
          ReadOnlyRow(
            title: 'Model',
            value: agent?.model.isNotEmpty == true
                ? agent!.model
                : (state.modelName ?? 'Not configured'),
            description: 'Set on the server; the app follows it.',
          ),
          ReadOnlyRow(title: 'Provider', value: agent?.provider.isNotEmpty == true ? agent!.provider : '—'),
          ReadOnlyRow(
            title: 'Context window',
            value: (agent?.contextWindowTokens ?? 0) > 0
                ? _tokens(agent!.contextWindowTokens)
                : '—',
          ),
          ReadOnlyRow(
            title: 'Max output',
            value: (agent?.maxTokens ?? 0) > 0 ? _tokens(agent!.maxTokens) : '—',
          ),
          ReadOnlyRow(
            title: 'Temperature',
            value: agent == null ? '—' : agent.temperature.toString(),
          ),
          ReadOnlyRow(
            title: 'Reasoning effort',
            value: agent?.reasoningEffort.isNotEmpty == true
                ? agent!.reasoningEffort
                : '—',
          ),
        ],
      ),
      const SizedBox(height: 22),
      SettingsSectionTitle('Model call order'),
      SettingsGroup(
        children: [
          if (order.isEmpty)
            const SettingsRow(
              title: 'No override',
              description: 'The server uses its own default ordering.',
            )
          else
            for (var i = 0; i < order.length; i++)
              ReadOnlyRow(title: '${i + 1}', value: order[i]),
        ],
      ),
    ];
  }

  List<Widget> _image(BuildContext context) {
    final state = context.watch<AppState>();
    final s = state.settings?.imageGeneration;
    return [
      SettingsSectionTitle('Image generation'),
      SettingsGroup(
        children: [
          SettingsRow(
            title: 'Enabled',
            description: 'Let the agent create images during a turn.',
            child: _busyOrToggle(
              key: 'image',
              value: s?.enabled ?? false,
              onChanged: (v) => _run('image', (mutate) async {
                await state.settingsApi
                    .updateImageGeneration(mutate, s!, enabled: v);
              }),
            ),
          ),
          ReadOnlyRow(title: 'Provider', value: s?.provider.isNotEmpty == true ? s!.provider : '—'),
          ReadOnlyRow(title: 'Model', value: s?.model.isNotEmpty == true ? s!.model : '—'),
          ReadOnlyRow(
            title: 'Credentials',
            value: (s?.providerConfigured ?? false) ? 'Configured' : 'Missing',
            description: (s?.providerConfigured ?? false)
                ? null
                : 'Add an API key on the server to switch this on.',
          ),
          ReadOnlyRow(title: 'Aspect ratio', value: s?.defaultAspectRatio ?? '—'),
          ReadOnlyRow(title: 'Image size', value: s?.defaultImageSize ?? '—'),
          ReadOnlyRow(
            title: 'Images per turn',
            value: '${s?.maxImagesPerTurn ?? 0}',
          ),
        ],
      ),
      if ((s?.providers.length ?? 0) > 0) ...[
        const SizedBox(height: 22),
        SettingsSectionTitle('Available providers'),
        SettingsGroup(
          children: [
            for (final prov in s!.providers)
              ReadOnlyRow(
                title: prov.label,
                value: prov.configured == true ? 'Configured' : 'Not configured',
              ),
          ],
        ),
      ],
    ];
  }

  List<Widget> _voice(BuildContext context) {
    final state = context.watch<AppState>();
    final s = state.settings?.transcription;
    return [
      SettingsSectionTitle('Voice notes'),
      SettingsGroup(
        children: [
          SettingsRow(
            title: 'Transcription',
            description: 'Turn a recorded voice note into text in the composer.',
            child: _busyOrToggle(
              key: 'voice',
              value: s?.enabled ?? false,
              onChanged: (v) => _run('voice', (mutate) async {
                await state.settingsApi.updateTranscription(mutate, s!, enabled: v);
              }),
            ),
          ),
          ReadOnlyRow(title: 'Provider', value: s?.provider.isNotEmpty == true ? s!.provider : '—'),
          ReadOnlyRow(title: 'Model', value: s?.model.isNotEmpty == true ? s!.model : '—'),
          ReadOnlyRow(title: 'Language', value: s?.language ?? 'Auto-detect'),
          ReadOnlyRow(
            title: 'Max duration',
            value: '${s?.maxDurationSec ?? 0} s',
          ),
          ReadOnlyRow(title: 'Max upload', value: '${s?.maxUploadMb ?? 0} MB'),
        ],
      ),
    ];
  }

  List<Widget> _browser(BuildContext context) {
    final state = context.watch<AppState>();
    final p = context.palette;
    final s = state.settings?.webSearch;
    final providers = s?.providers ?? const [];
    return [
      SettingsSectionTitle('Web search'),
      SettingsGroup(
        children: [
          if (providers.isEmpty)
            ReadOnlyRow(
              title: 'Provider',
              value: s?.provider.isNotEmpty == true ? s!.provider : '—',
            )
          else
            for (final prov in providers)
              SettingsRow(
                title: prov.label,
                description: prov.credential == 'none'
                    ? 'No key required'
                    : prov.credential == 'base_url'
                        ? 'Needs a base URL'
                        : 'Needs an API key',
                child: s?.provider == prov.name
                    ? Icon(Icons.check_rounded, size: 18, color: p.primary)
                    : TextButton(
                        onPressed: _busyOrToggleValue('browser')
                            ? null
                            : () => _run('browser', (mutate) async {
                                  await state.settingsApi.updateWebSearch(
                                    mutate,
                                    s!,
                                    provider: prov.name,
                                  );
                                }),
                        child: const Text('Use'),
                      ),
              ),
          SettingsRow(
            title: 'Read pages with Jina Reader',
            description:
                'Falls back to a readable-text extraction when a page blocks scraping.',
            child: _busyOrToggle(
              key: 'jina',
              value: s?.useJinaReader ?? false,
              onChanged: (v) => _run('jina', (mutate) async {
                await state.settingsApi
                    .updateWebSearch(mutate, s!, useJinaReader: v);
              }),
            ),
          ),
          ReadOnlyRow(title: 'Max results', value: '${s?.maxResults ?? 0}'),
          ReadOnlyRow(title: 'Timeout', value: '${s?.timeout ?? 0} s'),
        ],
      ),
    ];
  }

  List<Widget> _channels(BuildContext context) {
    final state = context.watch<AppState>();
    final requests = state.pairing;
    return [
      SettingsSectionTitle('Pairing requests'),
      if (requests.isEmpty)
        const SettingsGroup(
          children: [
            SettingsRow(
              title: 'Nothing waiting',
              description:
                  'When a chat channel asks to pair with this agent, the request '
                  'appears here for approval.',
            ),
          ],
        )
      else
        SettingsGroup(
          children: [
            for (final r in requests)
              SettingsRow(
                title: r.channel.isEmpty ? 'Pairing' : r.channel,
                description:
                    '${r.senderId}${r.expiresInSeconds == null ? '' : ' · expires in ${r.expiresInSeconds}s'}',
                child: Row(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    TextButton(
                      onPressed: _busyOrToggleValue('pair:${r.code}')
                          ? null
                          : () => _run('pair:${r.code}', (mutate) async {
                                await state.settingsApi.resolvePairing(
                                  mutate,
                                  r.code,
                                  approve: false,
                                );
                              }),
                      child: const Text('Deny'),
                    ),
                    const SizedBox(width: 4),
                    FilledButton(
                      onPressed: _busyOrToggleValue('pair:${r.code}')
                          ? null
                          : () => _run('pair:${r.code}', (mutate) async {
                                await state.settingsApi.resolvePairing(
                                  mutate,
                                  r.code,
                                  approve: true,
                                );
                              }),
                      child: const Text('Approve'),
                    ),
                  ],
                ),
              ),
          ],
        ),
      const SizedBox(height: 22),
      SettingsSectionTitle('Channels'),
      const SettingsGroup(
        children: [
          SettingsRow(
            title: 'Configured on the server',
            description:
                'Chat channels are wired up in the gateway config. Pairing above '
                'is what the phone can act on.',
          ),
        ],
      ),
    ];
  }

  List<Widget> _apps(BuildContext context) {
    final state = context.watch<AppState>();
    final apps = state.cliApps;
    final installed = apps.where((a) => a.installed).toList();
    final available = apps.where((a) => !a.installed).toList();
    return [
      SettingsSectionTitle('Installed (${installed.length})'),
      if (installed.isEmpty)
        const SettingsGroup(
          children: [
            SettingsRow(title: 'Nothing installed yet'),
          ],
        )
      else
        SettingsGroup(
          children: [
            for (final a in installed)
              SettingsRow(
                title: a.displayName,
                description: a.description.isEmpty ? a.category : a.description,
                child: OutlinedButton(
                  onPressed: _busyOrToggleValue('cli:${a.name}')
                      ? null
                      : () => _run('cli:${a.name}', (mutate) async {
                            await state.settingsApi.runCliAppAction(
                              mutate,
                              'test',
                              a.name,
                            );
                          }),
                  child: const Text('Test'),
                ),
              ),
          ],
        ),
      const SizedBox(height: 22),
      SettingsSectionTitle('Catalog'),
      SettingsGroup(
        children: [
          for (final a in available.take(25))
            SettingsRow(
              title: a.displayName,
              description: a.description.isEmpty ? a.category : a.description,
              child: OutlinedButton(
                onPressed: !a.installSupported ||
                        _busyOrToggleValue('cli:${a.name}')
                    ? null
                    : () => _run('cli:${a.name}', (mutate) async {
                          await state.settingsApi.runCliAppAction(
                            mutate,
                            'install',
                            a.name,
                          );
                        }),
                child: const Text('Install'),
              ),
            ),
        ],
      ),
    ];
  }

  List<Widget> _automations(BuildContext context) {
    final state = context.watch<AppState>();
    final jobs = state.automations;
    return [
      SettingsSectionTitle('Scheduled work'),
      if (jobs.isEmpty)
        const SettingsGroup(
          children: [
            SettingsRow(
              title: 'No automations',
              description: 'Ask the agent to schedule something and it lands here.',
            ),
          ],
        )
      else
        SettingsGroup(
          children: [
            for (final j in jobs)
              SettingsRow(
                title: j.title,
                description: [
                  if (j.schedule.isNotEmpty) j.schedule,
                  if (j.lastStatus.isNotEmpty) 'last run ${j.lastStatus}',
                  if (j.runCount > 0) '${j.runCount} runs',
                ].join(' · '),
                child: j.protected
                    ? SettingsStatusPill(
                        label: j.enabled ? 'On' : 'Off',
                        tone: j.enabled ? SettingsTone.good : SettingsTone.quiet,
                      )
                    : _busyOrToggle(
                        key: 'auto:${j.id}',
                        value: j.enabled,
                        onChanged: (v) => _run('auto:${j.id}', (mutate) async {
                          await state.settingsApi
                              .setAutomationEnabled(mutate, j.id, enabled: v);
                        }),
                      ),
              ),
          ],
        ),
    ];
  }

  List<Widget> _skills(BuildContext context) {
    final state = context.watch<AppState>();
    final skills = state.skills;
    return [
      SettingsSectionTitle('Skills (${skills.length})'),
      if (skills.isEmpty)
        const SettingsGroup(
          children: [SettingsRow(title: 'No skills found')],
        )
      else
        SettingsGroup(
          children: [
            for (final s in skills)
              SettingsRow(
                title: s.name,
                description: s.description,
                child: _busyOrToggle(
                  key: 'skill:${s.name}',
                  value: s.enabled,
                  onChanged: (v) => _run('skill:${s.name}', (mutate) async {
                    await state.settingsApi
                        .setSkillEnabled(mutate, s.name, enabled: v);
                  }),
                ),
              ),
          ],
        ),
    ];
  }

  List<Widget> _advanced(BuildContext context) {
    final state = context.watch<AppState>();
    final adv = state.settings?.advanced;
    final features = state.features;
    final enabled = features.where((f) => f.enabled).length;
    return [
      SettingsSectionTitle('Network safety'),
      SettingsGroup(
        children: [
          SettingsRow(
            title: 'Allow access to local services',
            description:
                'Let the agent reach services on the host network — needed for '
                'local model servers and previews.',
            child: _busyOrToggle(
              key: 'network',
              value: adv?.webuiAllowLocalServiceAccess ?? false,
              onChanged: (v) => _run('network', (mutate) async {
                await state.settingsApi.updateAdvanced(
                  mutate,
                  adv!,
                  webuiAllowLocalServiceAccess: v,
                );
              }),
            ),
          ),
          ReadOnlyRow(
            title: 'Restrict to workspace',
            value: (adv?.restrictToWorkspace ?? false) ? 'On' : 'Off',
          ),
          ReadOnlyRow(
            title: 'Default access mode',
            value: adv?.webuiDefaultAccessMode ?? '—',
          ),
          ReadOnlyRow(
            title: 'MCP servers',
            value: '${adv?.mcpServerCount ?? 0}',
          ),
          ReadOnlyRow(
            title: 'Shell execution',
            value: (adv?.execEnabled ?? false) ? 'Enabled' : 'Disabled',
          ),
        ],
      ),
      const SizedBox(height: 22),
      SettingsSectionTitle('Server features ($enabled/${features.length})'),
      SettingsGroup(
        children: [
          for (final f in features)
            SettingsRow(
              title: f.displayName,
              description: f.requiresRestart
                  ? '${_featureStatus(f)} · needs a server restart'
                  : _featureStatus(f),
              child: _busyOrToggle(
                key: 'feat:${f.name}',
                value: f.enabled,
                onChanged: f.installed
                    ? (v) => _run('feat:${f.name}', (mutate) async {
                          await state.settingsApi
                              .setFeature(mutate, f.name, enabled: v);
                        })
                    : null,
              ),
            ),
        ],
      ),
      const SizedBox(height: 22),
      SettingsSectionTitle('Advanced'),
      SettingsGroup(
        children: [
          SettingsRow(
            title: 'Check for updates',
            description: 'Ask the server whether a newer build is published.',
            onTap: () async {
              final v = await state.settingsApi
                  .checkForUpdate(state.apiToken ?? '', state.accessToken);
              if (!context.mounted) return;
              ScaffoldMessenger.of(context).showSnackBar(
                SnackBar(
                  content: Text(
                    v == null
                        ? 'You are on the latest server build.'
                        : 'Server update available: $v',
                  ),
                ),
              );
            },
          ),
          ReadOnlyRow(
            title: 'MCP presets',
            value: '${state.mcpPresets.length} available',
            description: '${state.mcpPresets.where((m) => m.installed).length} installed',
          ),
        ],
      ),
      const SizedBox(height: 22),
      const _SignOutButton(),
    ];
  }

  String _featureStatus(NanobotFeature f) {
    if (f.enabled) return 'Enabled';
    switch (f.status) {
      case 'missing_dependency':
        return 'Not installed';
      case 'not_installed':
        return 'Not installed';
      case 'unconfigured':
        return 'Needs configuration';
      default:
        return f.status.isEmpty ? 'Off' : f.status.replaceAll('_', ' ');
    }
  }

  /// A toggle that shows a spinner in place of the thumb while its write is in
  /// flight, and refuses taps until it lands.
  Widget _busyOrToggle({
    required String key,
    required bool value,
    ValueChanged<bool>? onChanged,
  }) {
    if (_isBusy(key)) return const _InlineSpinner();
    return WebToggle(value: value, onChanged: onChanged);
  }

  bool _busyOrToggleValue(String key) => _isBusy(key);
}


class _InlineSpinner extends StatelessWidget {
  const _InlineSpinner();
  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    return SizedBox(
      width: 38,
      height: WebSpace.touchTarget,
      child: Center(
        child: SizedBox(
          width: 16,
          height: 16,
          child: CircularProgressIndicator(strokeWidth: 2, color: p.primary),
        ),
      ),
    );
  }
}

/// The web's "Back to chat" pill, shown at the top of every non-overview page.
class _BackToChatPill extends StatelessWidget {
  const _BackToChatPill();
  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    return Padding(
      padding: const EdgeInsets.only(bottom: 18),
      child: Align(
        alignment: Alignment.centerLeft,
        child: OutlinedButton.icon(
          onPressed: () => Navigator.of(context).maybePop(),
          icon: const Icon(Icons.arrow_back_rounded, size: 16),
          label: const Text('Back to chat'),
          style: OutlinedButton.styleFrom(
            minimumSize: const Size(0, 34),
            padding: const EdgeInsets.symmetric(horizontal: 14),
            side: BorderSide(color: p.input),
            shape: const StadiumBorder(),
            textStyle: const TextStyle(fontSize: 13, fontWeight: FontWeight.w500),
          ),
        ),
      ),
    );
  }
}

class _SwatchRow extends StatelessWidget {
  const _SwatchRow({
    required this.title,
    required this.description,
    required this.swatches,
  });

  final String title;
  final String description;
  final List<Color> swatches;

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    return SettingsRow(
      title: title,
      description: description,
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          for (final c in swatches) ...[
            Container(
              width: 26,
              height: 26,
              decoration: BoxDecoration(
                color: c,
                borderRadius: BorderRadius.circular(WebRadii.compact),
                border: Border.all(color: p.border),
              ),
            ),
            if (c != swatches.last) const SizedBox(width: 6),
          ],
        ],
      ),
    );
  }
}

class _SignOutButton extends StatelessWidget {
  const _SignOutButton();
  @override
  Widget build(BuildContext context) {
    return SizedBox(
      width: double.infinity,
      child: OutlinedButton.icon(
        onPressed: () async {
          await context.read<AppState>().signOut();
          if (context.mounted) {
            Navigator.of(context).pushNamedAndRemoveUntil('/auth', (r) => false);
          }
        },
        icon: const Icon(Icons.logout_rounded, size: 18),
        label: const Text('Sign out'),
        style: OutlinedButton.styleFrom(
          foregroundColor: context.palette.destructive,
          minimumSize: const Size.fromHeight(46),
          side: BorderSide(
            color: context.palette.destructive.withValues(alpha: 0.35),
          ),
        ),
      ),
    );
  }
}

/// 1.2K / 3.4M — the same shorthand the usage surfaces use.
String _tokens(int value) {
  if (value >= 1000000) return '${(value / 1000000).toStringAsFixed(1)}M';
  if (value >= 1000) return '${(value / 1000).toStringAsFixed(1)}K';
  return '$value';
}
