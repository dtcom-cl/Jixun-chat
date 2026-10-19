
class LingLine {
  const LingLine({required this.emotion, required this.text, required this.action});

  /// 情绪标签（不带【】）；没有标签时为空串。
  final String emotion;

  /// 去掉标签和动作之后的正文。
  final String text;

  /// 末尾括号里的动作；没有则为空串。
  final String action;

  /// 有没有按规范写（给 UI 决定要不要画标签用）。
  bool get hasEmotion => emotion.isNotEmpty;
}

/// 匹配开头的【情绪】。
final RegExp _emotionHead = RegExp(r'^【([^】]{1,8})】\s*');

/// 匹配**结尾**的（动作），全角或半角括号都认。
final RegExp _actionTail = RegExp(r'([（(])([^）)]*)([）)])\s*$');

/// 中文（用来判断半角括号里是不是动作 —— 见下面的说明）。
final RegExp _cjk = RegExp(r'[\u4e00-\u9fff]');

/// 把一条消息解析成 情绪 / 正文 / 动作。
LingLine parseLingLine(String raw) {
  String body = raw.trim();

  String emotion = '';
  final Match? head = _emotionHead.firstMatch(body);
  if (head != null) {
    emotion = head.group(1)!.trim();
    body = body.substring(head.end);
  }

  String action = '';
  final Match? tail = _actionTail.firstMatch(body);
  if (tail != null) {
    final String opener = tail.group(1)!;
    final String inner = tail.group(2)!.trim();
    // 全角括号一律当动作；半角括号要求里面是中文，
    // 否则那是代码（qsort(a)、a[i]），不能当动作、更不能从正文里删掉。
    if (inner.isNotEmpty && (opener == '（' || _cjk.hasMatch(inner))) {
      action = inner;
      body = body.substring(0, tail.start).trim();
    }
  }

  return LingLine(emotion: emotion, text: body.trim(), action: action);
}

/// 这条消息是不是「LingChat 格式的台词」（有一条就画标签）。
bool isLingLine(String raw) => _emotionHead.hasMatch(raw.trim());
