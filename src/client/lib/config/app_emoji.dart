// 表情目录与解析（用户 2026-09-25 要求）。
//
// 规则：
// - 共 20 个表情，格式是 [笑哭] 这种“中括号 + 文字”；
// - 界面上显示的是 SVG 图片，不是 [笑哭] 文本；
// - 实际发送给 AI 的是 [笑哭] 这段文本；
// - 消息里出现 [xx] 且能在这 20 个里一一匹配上，就渲染成图片；
//   匹配不上就原样显示字符文本，绝不显示占位图或问号。
//
// 这里只负责“文本 -> 片段”的纯逻辑，不涉及任何 Widget，便于单测。

/// 一个表情：协议文本 + 本地 SVG 资源路径。
class AppEmoji {
  const AppEmoji(this.code, this.asset);

  /// 发送给 AI 的文本，例如 `[笑哭]`。
  final String code;

  /// 本地 SVG 资源，例如 `assets/emoji/xiao_ku.svg`。禁止网络加载。
  final String asset;
}

/// 全部 20 个表情，顺序即表情面板里的展示顺序。
const List<AppEmoji> kAppEmojis = <AppEmoji>[
  AppEmoji('[微笑]', 'assets/emoji/wei_xiao.svg'),
  AppEmoji('[大笑]', 'assets/emoji/da_xiao.svg'),
  AppEmoji('[笑哭]', 'assets/emoji/xiao_ku.svg'),
  AppEmoji('[流泪]', 'assets/emoji/liu_lei.svg'),
  AppEmoji('[生气]', 'assets/emoji/sheng_qi.svg'),
  AppEmoji('[惊讶]', 'assets/emoji/jing_ya.svg'),
  AppEmoji('[害羞]', 'assets/emoji/hai_xiu.svg'),
  AppEmoji('[无语]', 'assets/emoji/wu_yu.svg'),
  AppEmoji('[思考]', 'assets/emoji/si_kao.svg'),
  AppEmoji('[酷]', 'assets/emoji/ku.svg'),
  AppEmoji('[爱心]', 'assets/emoji/ai_xin.svg'),
  AppEmoji('[鼓掌]', 'assets/emoji/gu_zhang.svg'),
  AppEmoji('[赞]', 'assets/emoji/zan.svg'),
  AppEmoji('[衰]', 'assets/emoji/shuai.svg'),
  AppEmoji('[疑问]', 'assets/emoji/yi_wen.svg'),
  AppEmoji('[汗]', 'assets/emoji/han.svg'),
  AppEmoji('[抓狂]', 'assets/emoji/zhua_kuang.svg'),
  AppEmoji('[睡觉]', 'assets/emoji/shui_jiao.svg'),
  AppEmoji('[得意]', 'assets/emoji/de_yi.svg'),
  AppEmoji('[委屈]', 'assets/emoji/wei_qu.svg'),
];

/// 表情查找表 + 解析工具。
class AppEmojiBook {
  AppEmojiBook._();

  static final Map<String, AppEmoji> _byCode = <String, AppEmoji>{
    for (final AppEmoji e in kAppEmojis) e.code: e,
  };

  /// 按协议文本查表情；查不到返回 null（调用方原样显示文本）。
  static AppEmoji? byCode(String code) => _byCode[code];

  /// 这段文本是不是一个已知表情。
  static bool isKnownCode(String code) => _byCode.containsKey(code);

  /// `[` 与 `]` 之间最多允许多少字符，用于限制正则回溯。
  /// 现有表情最长是 `[抓狂]`（2 个汉字）。
  static const int maxInnerLength = 8;

  static final RegExp _pattern = RegExp(
    r'\[[^\[\]\n]{1,' + maxInnerLength.toString() + r'}\]',
  );

  /// 把一段文本切成有序片段：纯文本片段 + 命中表情的片段。
  ///
  /// 未命中的 `[xx]` 会留在纯文本片段里，因此原样显示。
  static List<EmojiToken> tokenize(String input) {
    if (input.isEmpty) return const <EmojiToken>[];
    // 快速路径：连 `[` 都没有，整段都是纯文本。
    if (!input.contains('[')) {
      return <EmojiToken>[EmojiToken.plain(input)];
    }

    final List<EmojiToken> tokens = <EmojiToken>[];
    int cursor = 0;
    for (final RegExpMatch m in _pattern.allMatches(input)) {
      final String code = m.group(0)!;
      final AppEmoji? emoji = _byCode[code];
      if (emoji == null) continue; // 匹配不上：保持原样，交给后面的纯文本片段
      if (m.start > cursor) {
        tokens.add(EmojiToken.plain(input.substring(cursor, m.start)));
      }
      tokens.add(EmojiToken.picture(emoji, code));
      cursor = m.end;
    }
    if (cursor < input.length) {
      tokens.add(EmojiToken.plain(input.substring(cursor)));
    }
    if (tokens.isEmpty) tokens.add(EmojiToken.plain(input));
    return tokens;
  }
}

/// 文本片段：要么是纯文本，要么是一个已命中的表情。
class EmojiToken {
  const EmojiToken.plain(this.text)
      : emoji = null,
        code = '';

  const EmojiToken.picture(AppEmoji this.emoji, this.code) : text = '';

  /// 纯文本内容（命中表情时为空串）。
  final String text;

  /// 命中表情时的表情；纯文本片段为 null。
  final AppEmoji? emoji;

  /// 命中表情时的协议文本，例如 `[笑哭]`。
  final String code;

  bool get isEmoji => emoji != null;
}
