// 小红点组件：未读提示。
//
// 依据：未读消息显示红色圆点气泡，进入聊天框后清零。

import 'package:flutter/material.dart';

class RedDot extends StatelessWidget {
  const RedDot({super.key, required this.count});

  final int count;

  @override
  Widget build(BuildContext context) {
    if (count <= 0) return const SizedBox.shrink();
    final text = count > 99 ? '99+' : '$count';
    return Container(
      constraints: const BoxConstraints(minWidth: 18, minHeight: 18),
      padding: const EdgeInsets.symmetric(horizontal: 5),
      decoration: const BoxDecoration(
        color: Color(0xFFF5222D),
        borderRadius: BorderRadius.all(Radius.circular(9)),
      ),
      alignment: Alignment.center,
      child: Text(
        text,
        style: const TextStyle(color: Colors.white, fontSize: 11, height: 1.1),
      ),
    );
  }
}
