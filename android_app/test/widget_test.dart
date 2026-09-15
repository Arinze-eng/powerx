// Unit tests for the PowerX native client core logic (no network required).

import 'package:flutter_test/flutter_test.dart';
import 'package:powerx_android/config.dart';
import 'package:powerx_android/models.dart';

void main() {
  group('PowerXConfig', () {
    test('exposes an HTTPS origin without trailing slash', () {
      expect(PowerXConfig.origin.startsWith('https://'), isTrue);
      expect(PowerXConfig.origin.endsWith('/'), isFalse);
    });

    test('derives a wss:// WebSocket origin from the https base', () {
      expect(PowerXConfig.wsOrigin.startsWith('wss://'), isTrue);
    });
  });

  group('SessionSummary', () {
    test('splits channel-prefixed keys into chatId', () {
      final s = SessionSummary.fromJson({
        'key': 'websocket:abc-123',
        'title': 'Trip planning',
        'preview': 'hello',
        'updated_at': '2026-09-15T10:00:00Z',
      });
      expect(s.chatId, 'abc-123');
      expect(s.displayTitle, 'Trip planning');
    });

    test('falls back to "New chat" when title is empty', () {
      final s = SessionSummary.fromJson({'key': 'c1'});
      expect(s.chatId, 'c1');
      expect(s.displayTitle, 'New chat');
    });
  });

  group('ThreadTurn.parseWebuiThread', () {
    test('parses user/assistant turns with reasoning', () {
      final payload = {
        'turns': [
          {'user': 'hi', 'assistant': 'Hello!', 'reasoning': 'thinking…'},
        ]
      };
      final turns = ThreadTurn.parseWebuiThread(payload);
      expect(turns.length, 2);
      expect(turns.first.role, 'user');
      expect(turns.last.role, 'assistant');
      expect(turns.last.content, 'Hello!');
      expect(turns.last.reasoning, 'thinking…');
    });

    test('returns empty list for malformed payload', () {
      expect(ThreadTurn.parseWebuiThread(null), isEmpty);
      expect(ThreadTurn.parseWebuiThread({}), isEmpty);
    });
  });
}
