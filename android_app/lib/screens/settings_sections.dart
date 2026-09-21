/// The settings sections, in the WebUI's order.
///
/// Mirrors `webui/src/components/settings/SettingsSidebar.tsx` (which lists
/// overview, appearance, models, image, voice, browser, channels, advanced) plus
/// the three sections the sidebar's utility actions open
/// (`onOpenApps` / `onOpenSkills` / `onOpenAutomations`).
///
/// The web deliberately does not surface a "System/runtime" entry, and neither
/// do we — a phone cannot restart the gateway.
enum SettingsSection {
  overview('Overview'),
  appearance('Appearance'),
  models('Models'),
  image('Image'),
  voice('Voice'),
  browser('Browser'),
  channels('Channels'),
  apps('Apps'),
  automations('Automations'),
  skills('Skills'),
  advanced('Advanced');

  const SettingsSection(this.label);

  /// Sidebar / page-title text.
  final String label;

  /// Stable id, matching the `?section=` query value the web uses.
  String get id => name;

  static SettingsSection fromId(String? id) {
    if (id == null) return SettingsSection.overview;
    for (final s in SettingsSection.values) {
      if (s.name == id) return s;
    }
    return SettingsSection.overview;
  }
}
