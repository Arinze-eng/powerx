// Unit tests for the PowerX native client additions (no network required):
// activity steps, credit/payment models, bootstrap parsing, attachment wire.

import 'package:flutter_test/flutter_test.dart';
import 'package:powerx_android/models.dart';
import 'package:powerx_android/services/gateway_api.dart';


void main() {
  group('ActivityStep', () {
    test('maps tool names to icon keys', () {
      expect(ActivityStep(id: '1', name: 'read_file').iconKey, 'read');
      expect(ActivityStep(id: '2', name: 'write_file').iconKey, 'write');
      expect(ActivityStep(id: '3', name: 'web_search').iconKey, 'search');
      expect(ActivityStep(id: '4', name: 'run_command').iconKey, 'run');
      expect(ActivityStep(id: '5', name: 'generate_image').iconKey, 'image');
      expect(ActivityStep(id: '6', name: 'something_else').iconKey, 'generic');
    });

    test('isDone reflects terminal statuses', () {
      final s = ActivityStep(id: '1', name: 'tool', status: 'running');
      expect(s.isDone, isFalse);
      s.status = 'done';
      expect(s.isDone, isTrue);
      s.status = 'error';
      expect(s.isDone, isTrue);
    });
  });

  group('CreditInfo', () {
    test('sums daily + purchased + granted into total', () {
      final c = CreditInfo.fromRow({
        'daily_credits': 100,
        'purchased_credits': 250,
        'granted_credits': 700,
        'drain_rate': 2,
      })!;
      expect(c.total, 1050);
      expect(c.drainRate, 2);
    });

    test('coerces string numerics and clamps drain rate >= 1', () {
      final c = CreditInfo.fromRow({
        'daily_credits': '50',
        'purchased_credits': null,
        'granted_credits': 0,
        'drain_rate': 0,
      })!;
      expect(c.daily, 50);
      expect(c.purchased, 0);
      expect(c.total, 50);
      expect(c.drainRate, 1);
    });
  });

  group('PaymentPackage', () {
    test('parses from bootstrap json', () {
      final p = PaymentPackage.fromJson({
        'name': 'Popular',
        'slug': 'popular',
        'credits': 3500,
        'amount_usd': 5.0,
      });
      expect(p.name, 'Popular');
      expect(p.credits, 3500);
      expect(p.amountUsd, 5.0);
    });
  });

  group('GatewayBootstrap.fromJson', () {
    test('parses supabase payment packages and identity fields', () {
      final b = GatewayBootstrap.fromJson({
        'token': 'ws-tok',
        'api_token': 'api-tok',
        'ws_path': '/',
        'model_name': 'custom/nemotron',
        'user_email': 'me@x.com',
        'supabase_user_id': 'uid-1',
        'supabase': {
          'url': 'https://sb.co',
          'anon_key': 'anon',
          'payment': {
            'payment_url': 'https://flutterwave.com/pay/x',
            'packages': [
              {'name': 'Starter', 'slug': 'starter', 'credits': 1000, 'amount_usd': 1.5},
              {'name': 'Best Value', 'slug': 'best_value', 'credits': 7500, 'amount_usd': 10.0},
            ],
          },
        },
      });
      expect(b.apiToken, 'api-tok');
      expect(b.modelName, 'custom/nemotron');
      expect(b.userEmail, 'me@x.com');
      expect(b.supabaseUserId, 'uid-1');
      expect(b.paymentPackages.length, 2);
      expect(b.paymentPackages.first.credits, 1000);
      expect(b.paymentUrl, 'https://flutterwave.com/pay/x');
    });

    test('handles needs_auth payload without crashing', () {
      final b = GatewayBootstrap.fromJson({
        'needs_auth': 'supabase',
        'supabase': {'url': 'https://sb.co', 'anon_key': 'anon'},
      });
      expect(b.needsAuth, isTrue);
      expect(b.token, isEmpty);
      expect(b.paymentPackages, isEmpty);
    });
  });

  group('PendingAttachment.toWireMedia', () {
    test('prefers data_url for images', () {
      final a = PendingAttachment(
          id: '1', name: 'pic.png', kind: 'image', dataUrl: 'data:image/png;base64,AAA');
      final w = a.toWireMedia();
      expect(w['data_url'], 'data:image/png;base64,AAA');
      expect(w['url'], isNull);
      expect(w['name'], 'pic.png');
    });

    test('uses url for uploaded files', () {
      final a = PendingAttachment(
          id: '2', name: 'doc.pdf', kind: 'file', url: 'https://onlyfiles.com/x');
      final w = a.toWireMedia();
      expect(w['url'], 'https://onlyfiles.com/x');
      expect(w['data_url'], isNull);
    });

    test('empty map when nothing ready', () {
      final a = PendingAttachment(id: '3', name: 'x', kind: 'file');
      expect(a.toWireMedia(), isEmpty);
    });
  });

  group('ChatMessage', () {
    test('viewableMedia filters http urls only', () {
      final m = ChatMessage(id: '1', role: Role.assistant, media: [
        '/local/path/file.bin',
        'https://cdn/x.png',
        'http://y/z',
      ]);
      expect(m.viewableMedia, ['https://cdn/x.png', 'http://y/z']);
    });
  });

  group('ActivityStep.fromToolEvent', () {
    test('start phase -> running step with summarized args', () {
      final step = ActivityStep.fromToolEvent({
        'version': 1,
        'phase': 'start',
        'call_id': 'call-9',
        'name': 'read_file',
        'arguments': {'path': '/repo/main.dart'},
      })!;
      expect(step.id, 'call-9');
      expect(step.name, 'read_file');
      expect(step.detail, '/repo/main.dart');
      expect(step.status, 'running');
    });

    test('end phase -> done, error phase -> error', () {
      final done = ActivityStep.fromToolEvent(
          {'phase': 'end', 'name': 'web_search', 'call_id': 'c1'})!;
      expect(done.status, 'done');
      final err = ActivityStep.fromToolEvent(
          {'phase': 'error', 'name': 'run_command', 'call_id': 'c2'})!;
      expect(err.status, 'error');
    });

    test('returns null when name missing', () {
      expect(ActivityStep.fromToolEvent({'phase': 'start'}), isNull);
    });

    test('falls back to name+order id when call_id absent', () {
      final step = ActivityStep.fromToolEvent(
          {'phase': 'start', 'name': 'list_dir'}, order: 3)!;
      expect(step.id, 'list_dir-3');
    });

    test('summarizeArgs prefers path then command then query', () {
      expect(ActivityStep.summarizeArgs({'command': 'ls -la'}), 'ls -la');
      expect(
          ActivityStep.summarizeArgs({'query': 'weather today'}),
          'weather today');
      expect(ActivityStep.summarizeArgs(null), '');
    });

    test('long arg values are truncated', () {
      final long = 'x' * 100;
      final out = ActivityStep.summarizeArgs({'path': long});
      expect(out.length, lessThanOrEqualTo(80));
      expect(out.endsWith('…'), isTrue);
    });
  });
}
