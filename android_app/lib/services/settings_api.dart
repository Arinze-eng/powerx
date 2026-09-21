import 'dart:async';
import 'dart:convert';

import 'package:http/http.dart' as http;

import '../config.dart';
import 'gateway_api.dart' show ApiException;

/// Client for the gateway's settings surface.
///
/// Two rules learned from the real gateway, both of which the web client
/// already follows and the native app must match:
///
/// 1. **Reads are HTTP.** `GET /api/settings` and friends need *both* the
///    gateway bearer token (`Authorization`) and the Supabase access token
///    (`X-Nanobot-Auth`); without the second the server fails closed and
///    answers as an anonymous user.
/// 2. **Writes are socket.** Every path under `/api/settings/**` is in the
///    gateway's mutation allowlist, and plain HTTP mutations on those paths
///    answer `405 WebUI mutations require an authenticated WebSocket`. So a
///    toggle posts `{"type":"webui_request","action":"settings.web_search.update"}`
///    over the chat socket instead. [WebUiMutation] is that transport, so the
///    same client can be driven by the live socket or by a fake in tests.
typedef WebUiMutation = Future<Map<String, dynamic>> Function(
  String action,
  Map<String, dynamic> payload,
);

/// One selectable provider entry (`web_search.providers`, `transcription.providers`).
class ProviderOption {
  const ProviderOption({
    required this.name,
    required this.label,
    this.credential,
    this.configured,
    this.defaultModel,
    this.models = const [],
  });

  final String name;
  final String label;

  /// `none` | `api_key` | `optional_api_key` | `base_url` | `oauth`.
  final String? credential;
  final bool? configured;
  final String? defaultModel;
  final List<String> models;

  static ProviderOption fromJson(Map<String, dynamic> j) => ProviderOption(
    name: _str(j['name']),
    label: _str(j['label'], fallback: _str(j['name'])),
    credential: j['credential'] as String?,
    configured: j['configured'] is bool ? j['configured'] as bool : null,
    defaultModel: j['default_model'] as String?,
    models: (j['models'] as List?)?.whereType<String>().toList() ?? const [],
  );
}

class WebSearchSettings {
  const WebSearchSettings({
    this.provider = 'duckduckgo',
    this.apiKeyHint,
    this.baseUrl,
    this.maxResults = 5,
    this.timeout = 30,
    this.useJinaReader = true,
    this.providers = const [],
  });

  final String provider;
  final String? apiKeyHint;
  final String? baseUrl;
  final int maxResults;
  final int timeout;
  final bool useJinaReader;
  final List<ProviderOption> providers;

  static WebSearchSettings parse(Map<String, dynamic> j) {
    final web = j['web'] is Map ? Map<String, dynamic>.from(j['web'] as Map) : const {};
    final fetch = web['fetch'] is Map
        ? Map<String, dynamic>.from(web['fetch'] as Map)
        : const {};
    return WebSearchSettings(
      provider: _str(j['provider'], fallback: 'duckduckgo'),
      apiKeyHint: j['api_key_hint'] as String?,
      baseUrl: j['base_url'] as String?,
      maxResults: _int(j['max_results'], 5),
      timeout: _int(j['timeout'], 30),
      useJinaReader: fetch['use_jina_reader'] != false,
      providers: _options(j['providers']),
    );
  }

  Map<String, dynamic> toUpdate({String? provider, bool? useJinaReader, int? maxResults}) => {
    'provider': provider ?? this.provider,
    if (maxResults != null) 'max_results': maxResults,
    if (useJinaReader != null) 'use_jina_reader': useJinaReader,
  };
}

class ImageGenerationSettings {
  const ImageGenerationSettings({
    this.enabled = false,
    this.provider = '',
    this.model = '',
    this.providerConfigured = false,
    this.defaultAspectRatio = '1:1',
    this.defaultImageSize = '1K',
    this.maxImagesPerTurn = 4,
    this.providers = const [],
  });

  final bool enabled;
  final String provider;
  final String model;
  final bool providerConfigured;
  final String defaultAspectRatio;
  final String defaultImageSize;
  final int maxImagesPerTurn;
  final List<ProviderOption> providers;

  static ImageGenerationSettings parse(Map<String, dynamic> j) =>
      ImageGenerationSettings(
        enabled: j['enabled'] == true,
        provider: _str(j['provider']),
        model: _str(j['model']),
        providerConfigured: j['provider_configured'] == true,
        defaultAspectRatio: _str(j['default_aspect_ratio'], fallback: '1:1'),
        defaultImageSize: _str(j['default_image_size'], fallback: '1K'),
        maxImagesPerTurn: _int(j['max_images_per_turn'], 4),
        providers: _options(j['providers']),
      );

  Map<String, dynamic> toUpdate({bool? enabled, String? model, String? provider}) => {
    'enabled': enabled ?? this.enabled,
    'provider': provider ?? this.provider,
    'model': model ?? this.model,
    'default_aspect_ratio': defaultAspectRatio,
    'default_image_size': defaultImageSize,
    'max_images_per_turn': maxImagesPerTurn,
  };
}

class TranscriptionSettings {
  const TranscriptionSettings({
    this.enabled = false,
    this.provider = '',
    this.model = '',
    this.providerConfigured = false,
    this.language,
    this.maxDurationSec = 120,
    this.maxUploadMb = 25,
    this.providers = const [],
  });

  final bool enabled;
  final String provider;
  final String model;
  final bool providerConfigured;
  final String? language;
  final int maxDurationSec;
  final int maxUploadMb;
  final List<ProviderOption> providers;

  static TranscriptionSettings parse(Map<String, dynamic> j) =>
      TranscriptionSettings(
        enabled: j['enabled'] == true,
        provider: _str(j['provider']),
        model: _str(j['model']),
        providerConfigured: j['provider_configured'] == true,
        language: j['language'] as String?,
        maxDurationSec: _int(j['max_duration_sec'], 120),
        maxUploadMb: _int(j['max_upload_mb'], 25),
        providers: _options(j['providers']),
      );

  Map<String, dynamic> toUpdate({bool? enabled, String? language}) => {
    'enabled': enabled ?? this.enabled,
    'provider': provider,
    'model': model,
    'language': language ?? this.language,
    'max_duration_sec': maxDurationSec,
    'max_upload_mb': maxUploadMb,
  };
}

class AgentSettings {
  const AgentSettings({
    this.model = '',
    this.provider = '',
    this.resolvedProvider = '',
    this.hasApiKey = false,
    this.maxTokens = 0,
    this.contextWindowTokens = 0,
    this.temperature = 0,
    this.reasoningEffort = '',
    this.timezone = '',
  });

  final String model;
  final String provider;
  final String resolvedProvider;
  final bool hasApiKey;
  final int maxTokens;
  final int contextWindowTokens;
  final double temperature;
  final String reasoningEffort;
  final String timezone;

  static AgentSettings parse(Map<String, dynamic> j) => AgentSettings(
    model: _str(j['model']),
    provider: _str(j['provider']),
    resolvedProvider: _str(j['resolved_provider']),
    hasApiKey: j['has_api_key'] == true,
    maxTokens: _int(j['max_tokens'], 0),
    contextWindowTokens: _int(j['context_window_tokens'], 0),
    temperature: _double(j['temperature']),
    reasoningEffort: _str(j['reasoning_effort']),
    timezone: _str(j['timezone']),
  );
}

class AdvancedSettings {
  const AdvancedSettings({
    this.restrictToWorkspace = false,
    this.webuiAllowLocalServiceAccess = false,
    this.allowLocalPreviewAccess = false,
    this.webuiDefaultAccessMode = 'default',
    this.mcpServerCount = 0,
    this.execEnabled = false,
    this.privateServiceProtectionEnabled = false,
  });

  final bool restrictToWorkspace;
  final bool webuiAllowLocalServiceAccess;
  final bool allowLocalPreviewAccess;
  final String webuiDefaultAccessMode;
  final int mcpServerCount;
  final bool execEnabled;
  final bool privateServiceProtectionEnabled;

  static AdvancedSettings parse(Map<String, dynamic> j) => AdvancedSettings(
    restrictToWorkspace: j['restrict_to_workspace'] == true,
    webuiAllowLocalServiceAccess: j['webui_allow_local_service_access'] == true,
    allowLocalPreviewAccess: j['allow_local_preview_access'] == true,
    webuiDefaultAccessMode: _str(j['webui_default_access_mode'], fallback: 'default'),
    mcpServerCount: _int(j['mcp_server_count'], 0),
    execEnabled: j['exec_enabled'] == true,
    privateServiceProtectionEnabled: j['private_service_protection_enabled'] == true,
  );

  Map<String, dynamic> toUpdate({bool? webuiAllowLocalServiceAccess}) => {
    'webui_allow_local_service_access':
        webuiAllowLocalServiceAccess ?? this.webuiAllowLocalServiceAccess,
    'webui_default_access_mode': webuiDefaultAccessMode,
  };
}

class UsageSummary {
  const UsageSummary({
    this.totalTokens = 0,
    this.totalTokens30d = 0,
    this.totalTokens365d = 0,
    this.requests30d = 0,
    this.activeDays30d = 0,
    this.currentStreakDays = 0,
    this.longestStreakDays = 0,
    this.peakDayTokens = 0,
  });

  final int totalTokens;
  final int totalTokens30d;
  final int totalTokens365d;
  final int requests30d;
  final int activeDays30d;
  final int currentStreakDays;
  final int longestStreakDays;
  final int peakDayTokens;

  static UsageSummary parse(Map<String, dynamic> j) => UsageSummary(
    totalTokens: _int(j['total_tokens'], 0),
    totalTokens30d: _int(j['total_tokens_30d'], 0),
    totalTokens365d: _int(j['total_tokens_365d'], 0),
    requests30d: _int(j['requests_30d'], 0),
    activeDays30d: _int(j['active_days_30d'], 0),
    currentStreakDays: _int(j['current_streak_days'], 0),
    longestStreakDays: _int(j['longest_streak_days'], 0),
    peakDayTokens: _int(j['peak_day_tokens'], 0),
  );
}

class NanobotFeature {
  const NanobotFeature({
    required this.name,
    required this.displayName,
    this.installed = false,
    this.enabled = false,
    this.configured = false,
    this.ready = false,
    this.status = '',
    this.requiresRestart = false,
  });

  final String name;
  final String displayName;
  final bool installed;
  final bool enabled;
  final bool configured;
  final bool ready;
  final String status;
  final bool requiresRestart;

  static NanobotFeature fromJson(Map<String, dynamic> j) => NanobotFeature(
    name: _str(j['name']),
    displayName: _str(j['display_name'], fallback: _str(j['name'])),
    installed: j['installed'] == true,
    enabled: j['enabled'] == true,
    configured: j['configured'] == true,
    ready: j['ready'] == true,
    status: _str(j['status']),
    requiresRestart: j['requires_restart'] == true,
  );
}

class SkillInfo {
  const SkillInfo({
    required this.name,
    this.description = '',
    this.source = '',
    this.enabled = true,
    this.available = true,
    this.unavailableReason = '',
  });

  final String name;
  final String description;
  final String source;
  final bool enabled;
  final bool available;
  final String unavailableReason;

  static SkillInfo fromJson(Map<String, dynamic> j) => SkillInfo(
    name: _str(j['name']),
    description: _str(j['description']),
    source: _str(j['source']),
    enabled: j['enabled'] != false,
    available: j['available'] != false,
    unavailableReason: _str(j['unavailable_reason']),
  );
}

class McpPreset {
  const McpPreset({
    required this.name,
    required this.displayName,
    this.category = '',
    this.description = '',
    this.installed = false,
    this.configured = false,
    this.available = false,
    this.status = '',
    this.logoUrl = '',
    this.requires = '',
  });

  final String name;
  final String displayName;
  final String category;
  final String description;
  final bool installed;
  final bool configured;
  final bool available;
  final String status;
  final String logoUrl;
  final String requires;

  static McpPreset fromJson(Map<String, dynamic> j) => McpPreset(
    name: _str(j['name']),
    displayName: _str(j['display_name'], fallback: _str(j['name'])),
    category: _str(j['category']),
    description: _str(j['description']),
    installed: j['installed'] == true,
    configured: j['configured'] == true,
    available: j['available'] == true,
    status: _str(j['status']),
    logoUrl: _str(j['logo_url']),
    requires: _str(j['requires']),
  );
}

/// A catalog CLI app (`/api/settings/cli-apps`).
class CliAppInfo {
  const CliAppInfo({
    required this.name,
    required this.displayName,
    this.category = '',
    this.description = '',
    this.requires = '',
    this.installed = false,
    this.available = false,
    this.status = '',
    this.installSupported = false,
  });

  final String name;
  final String displayName;
  final String category;
  final String description;
  final String requires;
  final bool installed;
  final bool available;
  final String status;
  final bool installSupported;

  static CliAppInfo fromJson(Map<String, dynamic> j) => CliAppInfo(
    name: _str(j['name']),
    displayName: _str(j['display_name'], fallback: _str(j['name'])),
    category: _str(j['category']),
    description: _str(j['description']),
    requires: _str(j['requires']),
    installed: j['installed'] == true,
    available: j['available'] == true,
    status: _str(j['status']),
    installSupported: j['install_supported'] == true,
  );
}

/// A scheduled automation (`/api/webui/automations`).
class AutomationJob {
  const AutomationJob({
    required this.id,
    this.name = '',
    this.enabled = true,
    this.schedule = '',
    this.nextRunAtMs,
    this.lastStatus = '',
    this.lastRunAtMs,
    this.lastError,
    this.protected = false,
    this.runCount = 0,
  });

  final String id;
  final String name;
  final bool enabled;

  /// Human-readable cadence, derived from the `schedule` object.
  final String schedule;
  final int? nextRunAtMs;
  final String lastStatus;
  final int? lastRunAtMs;
  final String? lastError;
  final bool protected;
  final int runCount;

  String get title => name.trim().isEmpty ? id : name.trim();

  static AutomationJob fromJson(Map<String, dynamic> j) {
    final state = j['state'] is Map
        ? Map<String, dynamic>.from(j['state'] as Map)
        : const <String, dynamic>{};
    final history = state['run_history'] as List? ?? const [];
    return AutomationJob(
      id: _str(j['id']),
      name: _str(j['name']),
      enabled: j['enabled'] != false,
      schedule: _describeSchedule(j['schedule']),
      nextRunAtMs: state['next_run_at_ms'] is num
          ? (state['next_run_at_ms'] as num).toInt()
          : null,
      lastStatus: _str(state['last_status']),
      lastRunAtMs: state['last_run_at_ms'] is num
          ? (state['last_run_at_ms'] as num).toInt()
          : null,
      lastError: state['last_error'] as String?,
      protected: j['protected'] == true,
      runCount: history.length,
    );
  }
}

/// Turn the gateway's schedule object into something a person can read:
/// `every 30m`, `cron 0 9 * * *`, `once`.
String _describeSchedule(Object? raw) {
  if (raw is! Map) return '';
  final m = Map<String, dynamic>.from(raw);
  final kind = _str(m['kind']);
  switch (kind) {
    case 'every':
      final ms = m['every_ms'] is num ? (m['every_ms'] as num).toInt() : 0;
      if (ms <= 0) return 'every';
      if (ms % 3600000 == 0) return 'every ${ms ~/ 3600000}h';
      if (ms % 60000 == 0) return 'every ${ms ~/ 60000}m';
      return 'every ${ms ~/ 1000}s';
    case 'cron':
      return 'cron ${_str(m['expr'])}'.trim();
    case 'at':
      final at = m['at_ms'] is num ? (m['at_ms'] as num).toInt() : null;
      if (at == null) return 'once';
      final dt = DateTime.fromMillisecondsSinceEpoch(at, isUtc: true).toLocal();
      return 'once at ${dt.toIso8601String().substring(0, 16).replaceFirst('T', ' ')}';
    default:
      return kind;
  }
}

class PairingRequestInfo {
  const PairingRequestInfo({
    required this.code,
    this.channel = '',
    this.senderId = '',
    this.expiresInSeconds,
  });

  final String code;
  final String channel;
  final String senderId;
  final int? expiresInSeconds;

  static PairingRequestInfo fromJson(Map<String, dynamic> j) => PairingRequestInfo(
    code: _str(j['code']),
    channel: _str(j['channel']),
    senderId: _str(j['sender_id']),
    expiresInSeconds: j['expires_in_seconds'] is num
        ? (j['expires_in_seconds'] as num).toInt()
        : null,
  );
}

/// Everything `GET /api/settings` returns that the native surface shows.
class SettingsSnapshot {
  const SettingsSnapshot({
    this.agent = const AgentSettings(),
    this.webSearch = const WebSearchSettings(),
    this.imageGeneration = const ImageGenerationSettings(),
    this.transcription = const TranscriptionSettings(),
    this.advanced = const AdvancedSettings(),
    this.usage = const UsageSummary(),
    this.version = '',
    this.requiresRestart = false,
    this.modelCallOrder = const [],
    this.raw = const {},
  });

  final AgentSettings agent;
  final WebSearchSettings webSearch;
  final ImageGenerationSettings imageGeneration;
  final TranscriptionSettings transcription;
  final AdvancedSettings advanced;
  final UsageSummary usage;
  final String version;
  final bool requiresRestart;
  final List<String> modelCallOrder;
  final Map<String, dynamic> raw;

  static SettingsSnapshot parse(Map<String, dynamic> j) {
    Map<String, dynamic> sub(String k) =>
        j[k] is Map ? Map<String, dynamic>.from(j[k] as Map) : const {};
    return SettingsSnapshot(
      agent: AgentSettings.parse(sub('agent')),
      webSearch: WebSearchSettings.parse(sub('web_search')),
      imageGeneration: ImageGenerationSettings.parse(sub('image_generation')),
      transcription: TranscriptionSettings.parse(sub('transcription')),
      advanced: AdvancedSettings.parse(sub('advanced')),
      usage: UsageSummary.parse(sub('usage')),
      version: _str(sub('version')['current']),
      requiresRestart: j['requires_restart'] == true,
      modelCallOrder:
          (j['model_call_order'] as List?)?.map((e) => e.toString()).toList() ??
              const [],
      raw: j,
    );
  }
}

class AppVersionInfo {
  const AppVersionInfo({this.name = '', this.version = '', this.gitSha = ''});
  final String name;
  final String version;
  final String gitSha;

  static AppVersionInfo parse(Map<String, dynamic> j) => AppVersionInfo(
    name: _str(j['app']),
    version: _str(j['version']),
    gitSha: _str(j['git_sha']),
  );
}

/// REST + socket client for settings.
class SettingsApi {
  SettingsApi({http.Client? client, String? origin})
    : _client = client ?? http.Client(),
      origin = origin ?? PowerXConfig.origin;

  final http.Client _client;
  final String origin;

  /// Reads need the gateway bearer token and, in Supabase mode, the Supabase
  /// access token — the same pairing [GatewayApi] uses.
  Map<String, String> _headers(String apiToken, String? supabaseToken) => {
    'Authorization': 'Bearer $apiToken',
    'Cache-Control': 'no-store',
    'Accept': 'application/json',
    if (supabaseToken != null && supabaseToken.isNotEmpty)
      'X-Nanobot-Auth': supabaseToken,
  };

  Future<Map<String, dynamic>> _get(
    String path,
    String apiToken,
    String? supabaseToken,
  ) async {
    final res = await _client
        .get(Uri.parse('$origin$path'), headers: _headers(apiToken, supabaseToken))
        .timeout(const Duration(seconds: 45));
    if (res.statusCode == 401 || res.statusCode == 403) {
      throw ApiException(res.statusCode, 'Authentication required');
    }
    if (res.statusCode != 200) {
      throw ApiException(res.statusCode, 'GET $path failed: HTTP ${res.statusCode}');
    }
    final decoded = jsonDecode(utf8.decode(res.bodyBytes));
    if (decoded is Map<String, dynamic>) return decoded;
    if (decoded is Map) return Map<String, dynamic>.from(decoded);
    return const {};
  }

  Future<SettingsSnapshot> fetchSettings(
    String apiToken,
    String? supabaseToken,
  ) async => SettingsSnapshot.parse(await _get('/api/settings', apiToken, supabaseToken));

  Future<UsageSummary> fetchUsage(String apiToken, String? supabaseToken) async =>
      UsageSummary.parse(await _get('/api/settings/usage', apiToken, supabaseToken));

  Future<AppVersionInfo> fetchVersion(String apiToken, String? supabaseToken) async =>
      AppVersionInfo.parse(await _get('/api/version', apiToken, supabaseToken));

  Future<List<NanobotFeature>> fetchFeatures(
    String apiToken,
    String? supabaseToken,
  ) async {
    final j = await _get('/api/settings/nanobot-features', apiToken, supabaseToken);
    return (j['features'] as List? ?? const [])
        .whereType<Map>()
        .map((f) => NanobotFeature.fromJson(Map<String, dynamic>.from(f)))
        .toList();
  }

  Future<List<SkillInfo>> fetchSkills(String apiToken, String? supabaseToken) async {
    final j = await _get('/api/webui/skills', apiToken, supabaseToken);
    return (j['skills'] as List? ?? const [])
        .whereType<Map>()
        .map((s) => SkillInfo.fromJson(Map<String, dynamic>.from(s)))
        .toList();
  }

  Future<List<McpPreset>> fetchMcpPresets(String apiToken, String? supabaseToken) async {
    final j = await _get('/api/settings/mcp-presets', apiToken, supabaseToken);
    return (j['presets'] as List? ?? const [])
        .whereType<Map>()
        .map((p) => McpPreset.fromJson(Map<String, dynamic>.from(p)))
        .toList();
  }

  Future<List<CliAppInfo>> fetchCliApps(String apiToken, String? supabaseToken) async {
    final j = await _get('/api/settings/cli-apps', apiToken, supabaseToken);
    return (j['apps'] as List? ?? const [])
        .whereType<Map>()
        .map((a) => CliAppInfo.fromJson(Map<String, dynamic>.from(a)))
        .toList();
  }

  Future<List<AutomationJob>> fetchAutomations(
    String apiToken,
    String? supabaseToken,
  ) async {
    final j = await _get('/api/webui/automations', apiToken, supabaseToken);
    return (j['jobs'] as List? ?? const [])
        .whereType<Map>()
        .map((a) => AutomationJob.fromJson(Map<String, dynamic>.from(a)))
        .toList();
  }

  Future<List<PairingRequestInfo>> fetchPairing(
    String apiToken,
    String? supabaseToken,
  ) async {
    final j = await _get('/api/settings/pairing', apiToken, supabaseToken);
    return (j['requests'] as List? ?? const [])
        .whereType<Map>()
        .map((r) => PairingRequestInfo.fromJson(Map<String, dynamic>.from(r)))
        .toList();
  }

  /// Whether a newer release is published upstream.
  Future<String?> checkForUpdate(String apiToken, String? supabaseToken) async {
    final j = await _get('/api/settings/version-check', apiToken, supabaseToken);
    final update = j['updateAvailable'];
    if (update is! Map) return null;
    final latest = _str(update['latest_version']).trim();
    return latest.isEmpty ? null : latest;
  }

  // ---- Socket mutations -------------------------------------------------

  /// Web-search provider / reader toggle.
  Future<void> updateWebSearch(WebUiMutation mutate, WebSearchSettings current,
      {String? provider, bool? useJinaReader, int? maxResults}) {
    return mutate(
      'settings.web_search.update',
      current.toUpdate(
        provider: provider,
        useJinaReader: useJinaReader,
        maxResults: maxResults,
      ),
    );
  }

  Future<void> updateImageGeneration(
    WebUiMutation mutate,
    ImageGenerationSettings current, {
    bool? enabled,
    String? model,
    String? provider,
  }) {
    return mutate(
      'settings.image_generation.update',
      current.toUpdate(enabled: enabled, model: model, provider: provider),
    );
  }

  Future<void> updateTranscription(
    WebUiMutation mutate,
    TranscriptionSettings current, {
    bool? enabled,
    String? language,
  }) {
    return mutate(
      'settings.transcription.update',
      current.toUpdate(enabled: enabled, language: language),
    );
  }

  Future<void> updateAdvanced(
    WebUiMutation mutate,
    AdvancedSettings current, {
    bool? webuiAllowLocalServiceAccess,
  }) {
    return mutate(
      'settings.network_safety.update',
      current.toUpdate(webuiAllowLocalServiceAccess: webuiAllowLocalServiceAccess),
    );
  }

  Future<void> setFeature(
    WebUiMutation mutate,
    String name, {
    required bool enabled,
  }) {
    return mutate(
      enabled ? 'settings.feature.enable' : 'settings.feature.disable',
      {'name': name},
    );
  }

  Future<void> setSkillEnabled(
    WebUiMutation mutate,
    String name, {
    required bool enabled,
  }) {
    return mutate('skill.update', {'name': name, 'enabled': enabled});
  }

  Future<void> resolvePairing(
    WebUiMutation mutate,
    String code, {
    required bool approve,
  }) {
    return mutate(
      approve ? 'settings.pairing.approve' : 'settings.pairing.deny',
      {'code': code},
    );
  }

  /// `install` | `update` | `uninstall` | `test` for one catalog CLI app.
  Future<void> runCliAppAction(
    WebUiMutation mutate,
    String action,
    String name,
  ) {
    return mutate('settings.cli_app.$action', {'name': name});
  }

  Future<void> setAutomationEnabled(
    WebUiMutation mutate,
    String id, {
    required bool enabled,
  }) {
    return mutate(enabled ? 'automation.enable' : 'automation.disable', {'id': id});
  }

  Future<void> deleteAutomation(WebUiMutation mutate, String id) {
    return mutate('automation.delete', {'id': id});
  }
}

// ---- small defensive readers -------------------------------------------

String _str(Object? v, {String fallback = ''}) {
  if (v == null) return fallback;
  final s = v.toString();
  return s.isEmpty ? fallback : s;
}

int _int(Object? v, int fallback) {
  if (v is num) return v.toInt();
  if (v is String) return int.tryParse(v) ?? fallback;
  return fallback;
}

double _double(Object? v, [double fallback = 0]) {
  if (v is num) return v.toDouble();
  if (v is String) return double.tryParse(v) ?? fallback;
  return fallback;
}

List<ProviderOption> _options(Object? v) => (v as List? ?? const [])
    .whereType<Map>()
    .map((m) => ProviderOption.fromJson(Map<String, dynamic>.from(m)))
    .toList();
