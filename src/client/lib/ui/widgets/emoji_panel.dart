// 表情面板（用户 2026-09-25 要求）。
//
// 点“表情”按钮后展开，5 列 × 4 行，共 20 个 SVG 表情。
// 界面上显示图片；点一下把 [笑哭] 这样的文本插入输入框。

import 'package:flutter/material.dart';
import 'package:flutter_svg/flutter_svg.dart';

import '../../config/app_emoji.dart';

class EmojiPanel extends StatelessWidget {
  const EmojiPanel({super.key, required this.onPick, this.height = 188});

  /// 选中一个表情，回传协议文本（例如 `[笑哭]`）。
  final void Function(String code) onPick;

  final double height;

  @override
  Widget build(BuildContext context) {
    return Container(
      height: height,
      decoration: const BoxDecoration(
        color: Color(0xFFF7F7F7),
        border: Border(top: BorderSide(color: Color(0xFFE0E0E0))),
      ),
      child: GridView.builder(
        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 10),
        gridDelegate: const SliverGridDelegateWithFixedCrossAxisCount(
          crossAxisCount: 5,
          mainAxisSpacing: 8,
          crossAxisSpacing: 8,
        ),
        itemCount: kAppEmojis.length,
        itemBuilder: (BuildContext context, int index) {
          final AppEmoji e = kAppEmojis[index];
          return InkWell(
            borderRadius: BorderRadius.circular(8),
            onTap: () => onPick(e.code),
            child: Padding(
              padding: const EdgeInsets.all(6),
              child: SvgPicture.asset(e.asset, fit: BoxFit.contain),
            ),
          );
        },
      ),
    );
  }
}
