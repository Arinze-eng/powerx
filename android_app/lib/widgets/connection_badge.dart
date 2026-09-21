import 'package:flutter/material.dart';

import '../state/app_state.dart';
import '../theme/palette.dart';

/// The live chat-socket indicator from `webui/src/components/ConnectionBadge`.
///
/// A single 8px dot on a 32px round hit area: emerald when open, amber (and
/// pulsing) while connecting/reconnecting, destructive on error, muted when
/// idle or closed. Tapping surfaces the same wording as the web tooltip.
class ConnectionBadge extends StatefulWidget {
  const ConnectionBadge({super.key, required this.status, this.onTap});

  final AppSocketStatus status;
  final VoidCallback? onTap;

  static String labelFor(AppSocketStatus status) {
    switch (status) {
      case AppSocketStatus.idle:
        return 'Idle';
      case AppSocketStatus.connecting:
        return 'Connecting';
      case AppSocketStatus.open:
        return 'Connected';
      case AppSocketStatus.reconnecting:
        return 'Reconnecting';
      case AppSocketStatus.closed:
        return 'Disconnected';
      case AppSocketStatus.error:
        return 'Connection error';
    }
  }

  @override
  State<ConnectionBadge> createState() => _ConnectionBadgeState();
}

class _ConnectionBadgeState extends State<ConnectionBadge>
    with SingleTickerProviderStateMixin {
  late final AnimationController _c = AnimationController(
    vsync: this,
    duration: const Duration(milliseconds: 1200),
  );

  bool get _pulsing =>
      widget.status == AppSocketStatus.connecting ||
      widget.status == AppSocketStatus.reconnecting ||
      widget.status == AppSocketStatus.error;

  @override
  void initState() {
    super.initState();
    if (_pulsing) _c.repeat();
  }

  @override
  void didUpdateWidget(ConnectionBadge old) {
    super.didUpdateWidget(old);
    if (_pulsing && !_c.isAnimating) {
      _c.repeat();
    } else if (!_pulsing && _c.isAnimating) {
      _c.stop();
      _c.value = 0;
    }
  }

  @override
  void dispose() {
    _c.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    final Color color = switch (widget.status) {
      AppSocketStatus.open => p.success,
      AppSocketStatus.connecting ||
      AppSocketStatus.reconnecting => p.warning,
      AppSocketStatus.error => p.destructive,
      AppSocketStatus.idle || AppSocketStatus.closed => p.mutedForeground,
    };
    final label = ConnectionBadge.labelFor(widget.status);
    return Semantics(
      button: widget.onTap != null,
      label: label,
      child: Tooltip(
        message: label,
        child: InkResponse(
          onTap: widget.onTap,
          radius: 18,
          child: SizedBox(
            width: 32,
            height: 32,
            child: Center(
              child: SizedBox(
                width: 8,
                height: 8,
                child: AnimatedBuilder(
                  animation: _c,
                  builder: (context, _) {
                    final t = _c.value;
                    return Stack(
                      alignment: Alignment.center,
                      children: [
                        if (_pulsing)
                          Opacity(
                            opacity: (1 - t).clamp(0, 1) * 0.75,
                            child: Transform.scale(
                              scale: 1 + t * 1.6,
                              child: Container(
                                decoration: BoxDecoration(
                                  color: color,
                                  shape: BoxShape.circle,
                                ),
                              ),
                            ),
                          ),
                        Container(
                          decoration: BoxDecoration(
                            color: color,
                            shape: BoxShape.circle,
                          ),
                        ),
                      ],
                    );
                  },
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
}
