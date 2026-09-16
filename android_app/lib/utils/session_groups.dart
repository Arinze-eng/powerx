import '../models.dart';

/// One labelled cluster of conversations in the history drawer.
class SessionGroup {
  const SessionGroup({required this.label, required this.rows});

  final String label;
  final List<SessionSummary> rows;

  bool get isEmpty => rows.isEmpty;
}

/// Bucket labels, ordered as they appear in the drawer. Mirrors the familiar
/// recency grouping used by ChatGPT and Manus.
const kToday = 'Today';
const kYesterday = 'Yesterday';
const kPrevious7Days = 'Previous 7 days';
const kPrevious30Days = 'Previous 30 days';
const kOlder = 'Older';

/// Group conversations by recency relative to [now].
///
/// Pure function (no Flutter dependency) so it is cheap to unit-test.
///
/// Rules:
///   * Order within a bucket follows the incoming list order — the server is
///     the authority on recency, we only label.
///   * Empty buckets are dropped, so the drawer never shows a stray header.
///   * Conversations with no timestamp land in [kOlder]; they are still
///     reachable rather than silently hidden.
List<SessionGroup> groupSessions(
  List<SessionSummary> sessions, {
  DateTime? now,
}) {
  final ref = now ?? DateTime.now();
  final today = DateTime(ref.year, ref.month, ref.day);
  final yesterday = today.subtract(const Duration(days: 1));
  final last7 = today.subtract(const Duration(days: 7));
  final last30 = today.subtract(const Duration(days: 30));

  final buckets = <String, List<SessionSummary>>{
    kToday: [],
    kYesterday: [],
    kPrevious7Days: [],
    kPrevious30Days: [],
    kOlder: [],
  };

  for (final s in sessions) {
    final at = s.updatedAt?.toLocal();
    if (at == null) {
      buckets[kOlder]!.add(s);
      continue;
    }
    final day = DateTime(at.year, at.month, at.day);
    if (!day.isBefore(today)) {
      buckets[kToday]!.add(s);
    } else if (!day.isBefore(yesterday)) {
      buckets[kYesterday]!.add(s);
    } else if (!day.isBefore(last7)) {
      buckets[kPrevious7Days]!.add(s);
    } else if (!day.isBefore(last30)) {
      buckets[kPrevious30Days]!.add(s);
    } else {
      buckets[kOlder]!.add(s);
    }
  }

  return [
    for (final label in [
      kToday,
      kYesterday,
      kPrevious7Days,
      kPrevious30Days,
      kOlder,
    ])
      if (buckets[label]!.isNotEmpty)
        SessionGroup(label: label, rows: List.unmodifiable(buckets[label]!)),
  ];
}

/// Short human label for a conversation timestamp, e.g. `9:41 AM`, `Yesterday`,
/// `12 Mar`. Kept dependency-free (no intl) because it is display sugar only.
String relativeDayLabel(DateTime at, {DateTime? now}) {
  final ref = (now ?? DateTime.now()).toLocal();
  final local = at.toLocal();
  final today = DateTime(ref.year, ref.month, ref.day);
  final day = DateTime(local.year, local.month, local.day);
  final diff = today.difference(day).inDays;
  if (diff <= 0) {
    final h = local.hour % 12 == 0 ? 12 : local.hour % 12;
    final m = local.minute.toString().padLeft(2, '0');
    final ampm = local.hour < 12 ? 'AM' : 'PM';
    return '$h:$m $ampm';
  }
  if (diff == 1) return 'Yesterday';
  if (diff < 7) return '$diff days ago';
  const months = [
    'Jan',
    'Feb',
    'Mar',
    'Apr',
    'May',
    'Jun',
    'Jul',
    'Aug',
    'Sep',
    'Oct',
    'Nov',
    'Dec',
  ];
  return '${local.day} ${months[local.month - 1]}';
}

/// Case-insensitive filter over conversations, matching title or preview.
/// Returns [sessions] unchanged when the query is blank.
List<SessionSummary> filterSessions(
  List<SessionSummary> sessions,
  String query,
) {
  final q = query.trim().toLowerCase();
  if (q.isEmpty) return sessions;
  return sessions
      .where(
        (s) =>
            s.displayTitle.toLowerCase().contains(q) ||
            s.preview.toLowerCase().contains(q),
      )
      .toList();
}

/// Suggestions shown on the landing screen (Manus-style starting points).
/// Kept here so both the landing screen and any future surfaces share one list.
class StarterPrompt {
  const StarterPrompt({
    required this.icon,
    required this.title,
    required this.subtitle,
    required this.prompt,
  });

  final String icon;
  final String title;
  final String subtitle;
  final String prompt;
}

const List<StarterPrompt> starterPrompts = [
  StarterPrompt(
    icon: '🔍',
    title: 'Research a topic',
    subtitle: 'Compare options with sources',
    prompt:
        'Research the current state of small modular nuclear reactors and '
        'summarise the key players, costs, and open risks with sources.',
  ),
  StarterPrompt(
    icon: '📊',
    title: 'Analyse data',
    subtitle: 'Turn a spreadsheet into insight',
    prompt:
        'Analyse the dataset I attach and give me the trends, outliers, '
        'and a short written summary.',
  ),
  StarterPrompt(
    icon: '✍️',
    title: 'Draft a document',
    subtitle: 'Reports, plans, proposals',
    prompt:
        'Draft a one-page project proposal for a mobile app that helps '
        'teams track field inspections offline.',
  ),
  StarterPrompt(
    icon: '',
    title: 'Build something',
    subtitle: 'Scripts, tools, small apps',
    prompt:
        'Build a Python script that renames files in a folder by date and '
        'writes a summary CSV.',
  ),
];
