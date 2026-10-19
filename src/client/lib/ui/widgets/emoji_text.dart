// 表情文本渲染（用户 2026-09-25 要求）。
//
// 把消息文本里的 [笑哭] 渲染成 SVG 图片；匹配不上的 [xx] 原样显示文本。
// 只用本地资源（assets/emoji/），不走网络。

import 'package:flutter/material.dart';
import 'package:flutter_svg/flutter_svg.dart';

import '../../config/app_emoji.dart';

class EmojiText extends StatelessWidget {
  const EmojiText(
    this.text, {
    super.key,
    required this.style,
    this.emojiSize = 22,
  });

  final String text;
  final TextStyle style;

  /// 表情图片的边长，跟着正文字号走。
  final double emojiSize;

  @override
  Widget build(BuildContext context) {
    final List<EmojiToken> tokens = AppEmojiBook.tokenize(text);

    // 没有命中任何表情：直接当普通文本，保持最简渲染路径。
    final bool hasEmoji = tokens.any((EmojiToken t) => t.isEmoji);
    if (!hasEmoji) {
      return Text(text, style: style);
    }

    return RichText(
      text: TextSpan(
        style: style,
        children: <InlineSpan>[
          for (final EmojiToken t in tokens)
            if (t.isEmoji)
              WidgetSpan(
                alignment: PlaceholderAlignment.middle,
                child: Padding(
                  padding: const EdgeInsets.symmetric(horizontal: 1),
                  child: SizedBox(
                    width: emojiSize,
                    height: emojiSize,
                    child: SvgPicture.asset(
                      t.emoji!.asset,
                      // 资源缺失时也不显示红叉，退化成空白，不影响排版
                      fit: BoxFit.contain,
                    ),
                  ),
                ),
              )
            else
              TextSpan(text: t.text),
        ],
      ),
    );
  }
}
