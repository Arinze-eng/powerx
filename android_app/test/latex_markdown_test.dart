import 'package:flutter/material.dart';
import 'package:flutter_markdown/flutter_markdown.dart';
import 'package:flutter_math_fork/flutter_math.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:markdown/markdown.dart' as md;
import 'package:powerx_android/widgets/latex_markdown.dart';

String html(String s) =>
    md.markdownToHtml(s, extensionSet: kLatexExtensionSet).trim();

void main() {
  group('TeX delimiters (remark-math parity)', () {
    test(r'inline $...$ and \(...\) become latex elements', () {
      expect(html(r'Energy is $E=mc^2$ here'),
          contains('<latex display="false">E=mc^2</latex>'));
      expect(html(r'Energy is \(E=mc^2\) here'),
          contains('<latex display="false">E=mc^2</latex>'));
    });

    test(r'display $$...$$ and \[...\] are display math', () {
      expect(html(r'$$\int_0^1 x\,dx$$'),
          contains(r'<latex display="true">\int_0^1 x\,dx</latex>'));
      expect(html(r'\[a^2+b^2\]'),
          contains('<latex display="true">a^2+b^2</latex>'));
    });

    test('prices are not math', () {
      final out = html(r'It costs $5 and $10 total');
      expect(out, isNot(contains('<latex')));
      expect(out, contains(r'$5 and $10'));
    });

    test('an escaped dollar is a literal dollar', () {
      expect(html(r'Pay \$5 now'), isNot(contains('<latex')));
    });

    test('code spans keep their dollar signs', () {
      expect(html(r'Use `$x$` literally'), isNot(contains('<latex')));
    });
  });

  testWidgets('a reply with TeX renders Math widgets and keeps its text',
      (tester) async {
    await tester.pumpWidget(MaterialApp(
      home: Scaffold(
        body: MarkdownBody(
          data: 'Area is \$\\pi r^2\$.\n\n\$\$\\frac{a}{b}\$\$',
          extensionSet: kLatexExtensionSet,
          builders: kLatexBuilders,
        ),
      ),
    ));
    expect(find.byType(Math), findsNWidgets(2));
    expect(find.textContaining('Area is'), findsOneWidget);
  });

  testWidgets('malformed TeX shows its source instead of throwing',
      (tester) async {
    await tester.pumpWidget(MaterialApp(
      home: Scaffold(
        body: MarkdownBody(
          data: r'Broken $\frac{$ here',
          extensionSet: kLatexExtensionSet,
          builders: kLatexBuilders,
        ),
      ),
    ));
    expect(tester.takeException(), isNull);
  });
}
