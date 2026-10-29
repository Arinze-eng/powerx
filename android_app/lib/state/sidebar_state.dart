/// Server-side sidebar state, mirroring `webui/src/hooks/useSidebarState.ts`.
///
/// Pins, archives, renames and manual order are stored on the gateway, so the
/// web and the phone show the same list. Only the fields the native drawer
/// uses are modelled; unknown keys are preserved on save via [raw].
class SidebarState {
  const SidebarState({
    this.pinnedKeys = const [],
    this.archivedKeys = const [],
    this.sessionOrder = const [],
    this.titleOverrides = const {},
    this.showArchived = false,
    this.sort = 'updated_desc',
    this.raw = const {},
  });

  final List<String> pinnedKeys;
  final List<String> archivedKeys;
  final List<String> sessionOrder;
  final Map<String, String> titleOverrides;
  final bool showArchived;
  final String sort;

  /// The last payload from the server. Re-sent on save so fields the native
  /// client does not edit (workbench, tags, collapsed groups) are not wiped.
  final Map<String, dynamic> raw;

  static const SidebarState empty = SidebarState();

  bool isPinned(String key) => pinnedKeys.contains(key);
  bool isArchived(String key) => archivedKeys.contains(key);

  String? titleOverride(String key) {
    final t = titleOverrides[key]?.trim();
    return (t == null || t.isEmpty) ? null : t;
  }

  factory SidebarState.fromJson(Object? json) {
    if (json is! Map) return empty;
    final m = Map<String, dynamic>.from(json);
    List<String> strings(Object? v) {
      if (v is! List) return const [];
      final out = <String>[];
      for (final item in v) {
        if (item is! String) continue;
        final c = item.trim();
        if (c.isNotEmpty && !out.contains(c)) out.add(c);
      }
      return out;
    }

    Map<String, String> strMap(Object? v) {
      if (v is! Map) return const {};
      final out = <String, String>{};
      v.forEach((k, val) {
        if (k is! String || val is! String) return;
        final ck = k.trim();
        final cv = val.trim();
        if (ck.isNotEmpty && cv.isNotEmpty) out[ck] = cv;
      });
      return out;
    }

    final view = m['view'] is Map ? Map<String, dynamic>.from(m['view'] as Map) : <String, dynamic>{};
    const sorts = ['updated_desc', 'created_desc', 'title_asc', 'manual'];
    final sort = sorts.contains(view['sort']) ? view['sort'] as String : 'updated_desc';

    return SidebarState(
      pinnedKeys: strings(m['pinned_keys']),
      archivedKeys: strings(m['archived_keys']),
      sessionOrder: strings(m['session_order']),
      titleOverrides: strMap(m['title_overrides']),
      showArchived: view['show_archived'] == true,
      sort: sort,
      raw: m,
    );
  }

  /// Payload for `sidebar.update`. Keeps unknown server fields intact.
  Map<String, dynamic> toJson() {
    final out = Map<String, dynamic>.from(raw);
    out['schema_version'] = 1;
    out['pinned_keys'] = pinnedKeys;
    out['archived_keys'] = archivedKeys;
    out['session_order'] = sessionOrder;
    out['title_overrides'] = titleOverrides;
    final view = raw['view'] is Map ? Map<String, dynamic>.from(raw['view'] as Map) : <String, dynamic>{};
    view['show_archived'] = showArchived;
    view['sort'] = sort;
    out['view'] = view;
    out.putIfAbsent('project_name_overrides', () => <String, String>{});
    out.putIfAbsent('tags_by_key', () => <String, dynamic>{});
    out.putIfAbsent('collapsed_groups', () => <String, bool>{});
    out.putIfAbsent('workbench', () => {'version': 1, 'tabs': <String, dynamic>{}});
    return out;
  }

  SidebarState copyWith({
    List<String>? pinnedKeys,
    List<String>? archivedKeys,
    List<String>? sessionOrder,
    Map<String, String>? titleOverrides,
    bool? showArchived,
    String? sort,
  }) {
    return SidebarState(
      pinnedKeys: pinnedKeys ?? this.pinnedKeys,
      archivedKeys: archivedKeys ?? this.archivedKeys,
      sessionOrder: sessionOrder ?? this.sessionOrder,
      titleOverrides: titleOverrides ?? this.titleOverrides,
      showArchived: showArchived ?? this.showArchived,
      sort: sort ?? this.sort,
      raw: raw,
    );
  }

  /// Drop keys for sessions that no longer exist, as the web does.
  SidebarState pruned(Iterable<String> validKeys) {
    final valid = validKeys.toSet();
    return copyWith(
      pinnedKeys: pinnedKeys.where(valid.contains).toList(),
      archivedKeys: archivedKeys.where(valid.contains).toList(),
      sessionOrder: sessionOrder.where(valid.contains).toList(),
      titleOverrides: {
        for (final e in titleOverrides.entries)
          if (valid.contains(e.key)) e.key: e.value,
      },
    );
  }

  @override
  bool operator ==(Object other) =>
      other is SidebarState && other.toString() == toString();

  @override
  int get hashCode => toString().hashCode;

  @override
  String toString() => toJson().toString();
}
