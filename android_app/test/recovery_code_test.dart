// Recovery codes: the service calls that issue and reset with a code, and the
// copy gate on the code screen. Runs in the normal `flutter test` step.

import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:powerx_android/screens/recovery_code_screen.dart';
import 'package:powerx_android/services/supabase_auth.dart';

const _url = 'https://example.supabase.co';

SupabaseAuth _auth(http.Client client) =>
    SupabaseAuth(url: _url, anonKey: 'anon', client: client);

void main() {
  group('issueRecoveryCode', () {
    test('posts with the user token and returns the new code', () async {
      late http.Request seen;
      final client = MockClient((req) async {
        seen = req;
        return http.Response(
            jsonEncode({'ok': true, 'recovery_code': 'ABCD-EFGH'}), 200);
      });

      final code = await _auth(client).issueRecoveryCode('tok-123');

      expect(code, 'ABCD-EFGH');
      expect(seen.url.path, '/functions/v1/recovery-code-issue');
      expect(seen.headers['Authorization'], 'Bearer tok-123');
    });

    test('an account that already has a code surfaces the server message',
        () async {
      final client = MockClient((_) async => http.Response(
            jsonEncode({'ok': false, 'error': 'already has a code'}),
            409,
          ));

      expect(
        () => _auth(client).issueRecoveryCode('tok'),
        throwsA(isA<AuthException>()
            .having((e) => e.message, 'message', 'already has a code')),
      );
    });

    test('a malformed success body is an error, not a null code', () async {
      final client =
          MockClient((_) async => http.Response(jsonEncode({'ok': true}), 200));

      expect(
        () => _auth(client).issueRecoveryCode('tok'),
        throwsA(isA<AuthException>()),
      );
    });
  });

  group('resetPasswordWithCode', () {
    test('sends a normalised email and returns the replacement code', () async {
      late Map<String, dynamic> body;
      final client = MockClient((req) async {
        body = jsonDecode(req.body) as Map<String, dynamic>;
        expect(req.url.path, '/functions/v1/recovery-password-reset');
        return http.Response(
            jsonEncode({'ok': true, 'recovery_code': 'WXYZ-2345'}), 200);
      });

      final next = await _auth(client).resetPasswordWithCode(
          '  User@Example.COM ', 'ABCD-EFGH', 'newpass1');

      expect(next, 'WXYZ-2345');
      expect(body['email'], 'user@example.com');
      expect(body['code'], 'ABCD-EFGH');
      expect(body['password'], 'newpass1');
    });

    test('a wrong code returns the server message', () async {
      final client = MockClient((_) async => http.Response(
            jsonEncode({'ok': false, 'error': 'Invalid code or email.'}),
            400,
          ));

      expect(
        () => _auth(client).resetPasswordWithCode('a@b.co', 'x', 'newpass1'),
        throwsA(isA<AuthException>()
            .having((e) => e.message, 'message', 'Invalid code or email.')),
      );
    });
  });

  group('RecoveryCodeScreen copy gate', () {
    String? clipboard;

    setUp(() {
      clipboard = null;
      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
          .setMockMethodCallHandler(SystemChannels.platform, (call) async {
        if (call.method == 'Clipboard.setData') {
          clipboard = (call.arguments as Map)['text'] as String?;
        }
        return null;
      });
    });

    tearDown(() {
      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
          .setMockMethodCallHandler(SystemChannels.platform, null);
    });

    Future<void> pump(WidgetTester tester, {required VoidCallback onContinue}) {
      return tester.pumpWidget(MaterialApp(
        home: RecoveryCodeScreen(
          code: 'ABCD-EFGH-IJKL-MNOP-QRST',
          error: null,
          onContinue: onContinue,
        ),
      ));
    }

    testWidgets('continue stays disabled until the code is copied',
        (tester) async {
      var continued = 0;
      await pump(tester, onContinue: () => continued++);

      FilledButton button() =>
          tester.widget<FilledButton>(find.byType(FilledButton));
      expect(button().onPressed, isNull);

      await tester.tap(find.text('Copy code'));
      await tester.pump();

      expect(clipboard, 'ABCD-EFGH-IJKL-MNOP-QRST');
      expect(button().onPressed, isNotNull);

      await tester.tap(find.byType(FilledButton));
      await tester.pump();
      expect(continued, 1);
    });

    testWidgets('shows the admin contact warning', (tester) async {
      await pump(tester, onContinue: () {});
      expect(
        find.textContaining(kRecoveryAdminContact),
        findsOneWidget,
      );
    });

    testWidgets('an error shows retry and sign-out, not the code',
        (tester) async {
      var retried = 0;
      await tester.pumpWidget(MaterialApp(
        home: RecoveryCodeScreen(
          code: null,
          error: 'Could not create your recovery code.',
          onContinue: () {},
          onRetry: () => retried++,
          onSignOut: () {},
        ),
      ));

      expect(find.text('Could not create your recovery code.'), findsOneWidget);
      expect(find.byType(SelectableText), findsNothing);
      await tester.tap(find.text('Try again'));
      await tester.pump();
      expect(retried, 1);
      expect(find.text('Sign out'), findsOneWidget);
    });
  });
}
