// Server history rows carry media as {kind,url,name} objects; they must still
// reach the bubble as viewable http urls, or images vanish on reopen.
import 'package:flutter_test/flutter_test.dart';
import 'package:powerx_android/models.dart';

void main() {
  test('assistant history row keeps signed image urls from object media', () {
    final h = ThreadHistory.parse({
      'messages': [
        {
          'role': 'user',
          'turnPhase': 'user',
          'content': 'draw a cat',
          'turnId': 't1',
          'turnSeq': 1,
        },
        {
          'role': 'assistant',
          'turnPhase': 'answer',
          'kind': 'answer',
          'content': 'here',
          'turnId': 't1',
          'turnSeq': 2,
          'media': [
            {'kind': 'image', 'url': 'https://x.test/cat.png?sig=1', 'name': 'cat.png'},
          ],
        },
      ],
    });
    final answer = h.messages.lastWhere((m) => m.role == Role.assistant);
    expect(answer.viewableMedia, ['https://x.test/cat.png?sig=1']);
  });
}
