// A send must always open its own answer bubble, below its own question.
import 'package:flutter_test/flutter_test.dart';
import 'package:powerx_android/models.dart';
import 'package:powerx_android/services/chat_cache.dart';

void main() {
  group('beginLiveTurn (second send never lands in the first bubble)', () {
    test('the second answer is a new bubble below the second question', () {
      final firstAnswer = ChatMessage(
        id: 'live-1',
        role: Role.assistant,
        segments: ['first answer'],
        streaming: true,
        turnId: 't1',
      );
      final messages = [
        ChatMessage(id: 'u-1', role: Role.user, text: 'one', turnId: 't1'),
        firstAnswer,
        ChatMessage(id: 'u-2', role: Role.user, text: 'two', turnId: 't2'),
      ];

      final live = beginLiveTurn(
        messages: messages,
        stale: firstAnswer,
        turnId: 't2',
      );

      expect(identical(messages.last, live), isTrue);
      expect(live.turnId, 't2');
      expect(live.streaming, isTrue);
      expect(live.text, isEmpty);
      expect(identical(messages[1], firstAnswer), isTrue);
      expect(firstAnswer.text, 'first answer');
      expect(firstAnswer.streaming, isFalse);
    });

    test('a stale bubble settles its unfinished steps', () {
      final stale = ChatMessage(
        id: 'live-1',
        role: Role.assistant,
        streaming: true,
        turnId: 't1',
      );
      stale.reasoningStreaming = true;
      beginLiveTurn(
        messages: [stale],
        stale: stale,
        turnId: 't2',
      );
      expect(stale.reasoningStreaming, isFalse);
      expect(stale.streaming, isFalse);
    });

    test('with nothing stale, the bubble is just appended', () {
      final messages = [
        ChatMessage(id: 'u-1', role: Role.user, text: 'hi', turnId: 't1'),
      ];
      final live = beginLiveTurn(
        messages: messages,
        stale: null,
        turnId: 't1',
      );
      expect(messages, hasLength(2));
      expect(identical(messages.last, live), isTrue);
    });

    test('the new bubble is never adopted as an earlier turn tail', () {
      // Once bound to t2, a replayed running status for t1 must not pick it up.
      final messages = [
        ChatMessage(id: 'u-1', role: Role.user, text: 'one', turnId: 't1'),
        ChatMessage(
          id: 'a-1',
          role: Role.assistant,
          segments: ['done'],
          turnId: 't1',
        ),
        ChatMessage(id: 'u-2', role: Role.user, text: 'two', turnId: 't2'),
      ];
      beginLiveTurn(messages: messages, stale: null, turnId: 't2');
      expect(
        resumableTailFor(messages: messages, serverTurnId: 't1'),
        isNull,
      );
    });
  });
}
