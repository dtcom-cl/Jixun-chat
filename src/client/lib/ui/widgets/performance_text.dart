
/// 一条「演出段」：整段文字被星号包住时命中。
class PerformanceSegment {
  const PerformanceSegment(this.text, {required this.isEmotion});

  /// 去掉星号之后的正文。
  final String text;

  /// 是不是纯情绪标签（`*（委屈）*`）——和动作分开，方便以后换样式。
  final bool isEmotion;
}

/// 星号（半角 * 和全角 ＊）。
bool _isStar(String ch) => ch == '*' || ch == '＊';

/// 情绪标签的正则：整个正文就是一个括号里的短词。
final RegExp _emotionPattern = RegExp(r'^[（(][^（）()]{1,8}[）)]$');

/// 解析一条消息是不是演出段；不是就返回 null（按普通台词渲染）。
PerformanceSegment? parsePerformance(String raw) {
  final String s = raw.trim();
  if (s.length < 3) return null;
  if (!_isStar(s[0]) || !_isStar(s[s.length - 1])) return null;

  // 两头的星号可能不止一个（**动作**），整串剥掉。
  int start = 0;
  while (start < s.length && _isStar(s[start])) {
    start++;
  }
  int end = s.length;
  while (end > start && _isStar(s[end - 1])) {
    end--;
  }
  final String inner = s.substring(start, end).trim();

  // 空内容、或者里面还有星号（说明是「*动作* 台词 *动作*」这种混排）→ 不当演出段。
  if (inner.isEmpty || inner.contains('*') || inner.contains('＊')) return null;

  return PerformanceSegment(inner, isEmotion: _emotionPattern.hasMatch(inner));
}
