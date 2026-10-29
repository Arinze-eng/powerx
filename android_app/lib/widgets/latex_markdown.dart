import 'package:flutter/material.dart';
import 'package:flutter_markdown/flutter_markdown.dart';
import 'package:flutter_math_fork/flutter_math.dart';
import 'package:markdown/markdown.dart' as md;

import '../theme/palette.dart';

/// Web parity (remark-math + rehype-katex, MarkdownTextRenderer.tsx): TeX
/// between `$...$` or `\(...\)` is inline math; `$$...$$` or `\[...\]` is
/// display math. Dollar signs follow remark-math's rules, so prices such as
/// "$5 and $10" stay plain text.
const String kLatexTag = 'latex';

class _LatexSyntax extends md.InlineSyntax {
  _LatexSyntax(super.pattern, {required this.display});

  final bool display;

  @override
  bool onMatch(md.InlineParser parser, Match match) {
    final el = md.Element.text(kLatexTag, (match[1] ?? '').trim());
    el.attributes['display'] = display ? 'true' : 'false';
    parser.addNode(el);
    return true;
  }
}

/// Markdown extensions that add TeX. Put before GitHub-flavoured inline
/// syntaxes so `\(`, `\[` and `\$` win over the generic escape rule.
final md.ExtensionSet kLatexExtensionSet = md.ExtensionSet(
  md.ExtensionSet.gitHubFlavored.blockSyntaxes,
  <md.InlineSyntax>[
    _LatexSyntax(r'\$\$([\s\S]+?)\$\$', display: true),
    _LatexSyntax(r'\\\[([\s\S]+?)\\\]', display: true),
    _LatexSyntax(r'\\\(([\s\S]+?)\\\)', display: false),
    _LatexSyntax(r'\$(?![\s$])([^$\n]*[^\s$])\$(?!\d)', display: false),
    ...md.ExtensionSet.gitHubFlavored.inlineSyntaxes,
  ],
);

/// Pass as `builders:` alongside [kLatexExtensionSet].
final Map<String, MarkdownElementBuilder> kLatexBuilders = {
  kLatexTag: LatexBuilder(),
};

class LatexBuilder extends MarkdownElementBuilder {
  @override
  Widget? visitElementAfterWithContext(
    BuildContext context,
    md.Element element,
    TextStyle? preferredStyle,
    TextStyle? parentStyle,
  ) {
    final src = element.textContent.trim();
    if (src.isEmpty) return null;
    final display = element.attributes['display'] == 'true';
    final base = parentStyle ?? DefaultTextStyle.of(context).style;
    final math = Math.tex(
      src,
      mathStyle: display ? MathStyle.display : MathStyle.text,
      textStyle: base,
      // Malformed TeX shows its source instead of breaking the whole reply.
      onErrorFallback: (_) => Text(
        display ? '\$\$$src\$\$' : '\$$src\$',
        style: base.copyWith(color: Palette.danger),
      ),
    );
    if (!display) return math;
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 6),
      child: SingleChildScrollView(
        scrollDirection: Axis.horizontal,
        child: math,
      ),
    );
  }
}
