// 聊天气泡组件：承载正文与时间戳。
//
// 依据（聊天气泡、QQ界面复刻）、3.1.4（灰色小字时间戳）。
//
// 2026-09-25 改动：
// - 正文改用 EmojiText：把 [笑哭] 这类文本渲染成 SVG 表情图；匹配不上原样显示文本；
// - 支持图片消息（content_type = image，content 是 data URL）；
// - AI 头像与用户头像全部使用本地资源，不再使用 NetworkImage。
//

import 'dart:io';
import 'dart:typed_data';

import 'package:flutter/material.dart';
import 'package:intl/intl.dart';

import '../../config/app_config.dart';
import '../../models/message.dart';
import '../../services/avatar_cache.dart';
import 'emoji_text.dart';
import 'ling_text.dart';
import 'performance_text.dart';

class Bubble extends StatelessWidget {
  const Bubble({
    super.key,
    required this.message,
    required this.showTime,
    this.showSenderName = false,
    this.aiAvatarPath,
    this.peerUserId = 0,
  });

  final ChatMessage message;

  /// 这个会话里「对方」的 users.id（人类好友会话才有，其它会话为 0）。
  ///
  /// 2026-09-30 第十四轮：真人好友会话里双方都是 sender_type = user，
  /// 必须靠它区分「这条是对方发的还是我发的」，否则对面的消息会画到右边。
  final int peerUserId;

  /// 这个会话里“对方”的头像文件路径（来自会话设置里选的那张）。
  ///
  /// 2026-09-27 第七轮修正：以前气泡头像是写死的全局内置头像，
  /// 用户在聊天设置里换了头像，聊天气泡毫无变化 —— 用户实测反馈过。
  /// 现在由 ChatPage 把当前会话的头像传进来。
  final String? aiAvatarPath;

  /// 是否在本条气泡上方显示灰色小字时间戳（同一分钟内的连续消息只显示一次）。
  final bool showTime;

  /// 群聊里显示发言人名字（PRD 3.4.1：每个 AI 有独立人格，必须能看出谁在说话）。
  /// 单聊只有一个人，不显示，保持 QQ 单聊的原样。
  final bool showSenderName;

  /// 对方一侧（AI / 真人好友）这一条消息该用哪张头像。
  ///
  /// 2026-09-30 第十五轮：优先用消息自带的 [ChatMessage.senderAvatar]（服务端
  /// 按 sender_id 现读 ai_profiles / users），没有或不是服务端资源时才退回
  /// 会话头像 [aiAvatarPath]。
  String? _peerAvatarPath() {
    final String? fromMessage = message.senderAvatar;
    if (fromMessage != null && _looksLikeServerAvatar(fromMessage)) {
      return fromMessage;
    }
    return aiAvatarPath;
  }

  /// 是不是服务端头像（`/uploads/...` 或完整 http 地址）。
  ///
  /// 历史库里存过**手机本地路径**（第九轮修过的那个 bug），那种值在手机上
  /// 就是「没有头像」；让它顶掉会话头像只会更糟。
  static bool _looksLikeServerAvatar(String url) {
    return url.startsWith('/uploads/') ||
        url.startsWith('http://') ||
        url.startsWith('https://');
  }

  @override
  Widget build(BuildContext context) {
    // 2026-09-30 第十四轮：这里原来写的是 message.isUser，即
    // 「sender_type == user 就是我发的」——真人好友会话里双方都是 user，
    // 于是对面的消息被画到右边、配上我自己的头像（用户实测反馈）。
    // 归属必须按 isMineFor（sender_id / from_peer）判。
    final bool isUser = message.isMineFor(peerUserId);
    final Color bubbleColor = isUser ? const Color(0xFF0099FF) : Colors.white;
    final Color textColor = isUser ? Colors.white : const Color(0xFF1A1A1A);

    final PerformanceSegment? performance =
        isUser ? null : parsePerformance(message.text);

    final LingLine? ling = isUser ? null : (isLingLine(message.text) ? parseLingLine(message.text) : null);

    return Column(
      crossAxisAlignment:
          isUser ? CrossAxisAlignment.end : CrossAxisAlignment.start,
      children: <Widget>[
        if (showTime)
          Padding(
            padding: const EdgeInsets.symmetric(vertical: 8),
            child: Center(
              child: Text(
                DateFormat('HH:mm').format(message.timestamp),
                style: const TextStyle(color: Color(0xFF999999), fontSize: 11),
              ),
            ),
          ),
        Padding(
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 3),
          child: Row(
            mainAxisAlignment:
                isUser ? MainAxisAlignment.end : MainAxisAlignment.start,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: <Widget>[
              if (!isUser) ...<Widget>[
                // 2026-09-30 第十五轮：对方一侧的头像**优先用这条消息自己的
                // sender_avatar**（协议里本来就有，服务端按 sender_id 现读
                // ai_profiles/users）。
                //
                // 为什么：conv.avatarUrl 只有一张脸（群聊里是第一个成员的），
                // 群里所有 AI 都顶着同一个头像；而且给某个成员换了头像之后，
                // 群里的气泡也不会跟着变。sender_avatar 是每条消息各自的发送者，
                // 单聊/群聊/真人好友三种情况都对。
                // 只在它是服务端资源时才采用，避免历史库里那些「手机本地路径」
                // 顶掉本来能显示的头像。
                _AiAvatar(path: _peerAvatarPath()),
                const SizedBox(width: 8),
              ],
              Flexible(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: <Widget>[
                    // 群聊里每条 AI 消息上方标出是谁说的
                    if (showSenderName && !isUser)
                      Padding(
                        padding: const EdgeInsets.only(left: 2, bottom: 3),
                        child: Text(
                          message.senderName,
                          style: const TextStyle(
                              fontSize: 11, color: Color(0xFF999999)),
                        ),
                      ),
                    message.isImage
                        ? _ImageBubble(dataUrl: message.text)
                        : performance != null
                            ? _PerformanceText(segment: performance)
                            : ling != null
                                ? _LingBubble(
                                    line: ling,
                                    bubbleColor: bubbleColor,
                                    textColor: textColor,
                                  )
                                : Container(
                            padding: const EdgeInsets.symmetric(
                                horizontal: 12, vertical: 9),
                            decoration: BoxDecoration(
                              color: bubbleColor,
                              borderRadius: BorderRadius.circular(8),
                            ),
                            child: EmojiText(
                              message.text,
                              style: TextStyle(
                                color: textColor,
                                fontSize: 15,
                                height: 1.35,
                              ),
                              emojiSize: 22,
                            ),
                          ),
                  ],
                ),
              ),
              if (isUser) const SizedBox(width: 8),
              if (isUser) const _UserAvatar(),
            ],
          ),
        ),
      ],
    );
  }
}

/// 演出段正文（动作 / 旁白 / 情绪标签）。
///
class _PerformanceText extends StatelessWidget {
  const _PerformanceText({required this.segment});

  final PerformanceSegment segment;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(left: 2, top: 3, bottom: 3),
      child: EmojiText(
        segment.text,
        style: const TextStyle(
          color: Color(0xFF9E9E9E),
          fontSize: 11,
          height: 1.35,
          fontStyle: FontStyle.italic,
        ),
        emojiSize: 14,
      ),
    );
  }
}

/// LingChat 格式的一条台词：情绪标签 + 气泡正文 + 可选动作。
///
class _LingBubble extends StatelessWidget {
  const _LingBubble({
    required this.line,
    required this.bubbleColor,
    required this.textColor,
  });

  final LingLine line;
  final Color bubbleColor;
  final Color textColor;

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: <Widget>[
        if (line.hasEmotion)
          Padding(
            padding: const EdgeInsets.only(left: 2, bottom: 2),
            child: Text(
              line.emotion,
              style: const TextStyle(
                // 用户 2026-10-04：「不要在前面用【】表达情绪，而是缩小一些、
                // 用淡淡的灰色字体和斜体字，在句首表示」——
                // 所以这里只有情绪词本身（方括号在解析时就去掉了）、
                // 10px（正文 15px）、淡灰、斜体，贴在正文句首。
                fontSize: 10,
                height: 1.2,
                color: Color(0xFFBDBDBD),
                fontStyle: FontStyle.italic,
              ),
            ),
          ),
        if (line.text.isNotEmpty)
          Container(
            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 9),
            decoration: BoxDecoration(
              color: bubbleColor,
              borderRadius: BorderRadius.circular(8),
            ),
            child: EmojiText(
              line.text,
              style: TextStyle(color: textColor, fontSize: 15, height: 1.35),
              emojiSize: 22,
            ),
          ),
        if (line.action.isNotEmpty)
          Padding(
            padding: const EdgeInsets.only(left: 2, top: 3),
            child: EmojiText(
              line.action,
              style: const TextStyle(
                color: Color(0xFF9E9E9E),
                fontSize: 11,
                height: 1.35,
                fontStyle: FontStyle.italic,
              ),
              emojiSize: 14,
            ),
          ),
      ],
    );
  }
}

/// 图片气泡：data URL 直接解码成字节渲染，不联网。
///
/// 2026-09-30 第十四轮：改成 StatefulWidget 并**把解码结果缓存住**。
/// 原来每次 build 都 base64Decode 一次，得到一个**新的 Uint8List**；
/// Image.memory 的 MemoryImage 是按对象标识比较相等性的（不是按内容），
/// 于是每次重建都被当成「换了一张图」：旧帧被丢掉（gaplessPlayback 默认 false）
/// → 图片先消失再重新解码出现。用户报的「UI还一直闪烁」里，图片气泡那一下
/// 白闪就是这个（聊天页每次收到消息 / 头像下载完都会重建）。
class _ImageBubble extends StatefulWidget {
  const _ImageBubble({required this.dataUrl});

  final String dataUrl;

  @override
  State<_ImageBubble> createState() => _ImageBubbleState();
}

class _ImageBubbleState extends State<_ImageBubble> {
  Uint8List? _bytes;

  @override
  void initState() {
    super.initState();
    _bytes = ChatMessage.decodeDataUrl(widget.dataUrl);
  }

  @override
  void didUpdateWidget(_ImageBubble oldWidget) {
    super.didUpdateWidget(oldWidget);
    // 只有换了一条消息（data URL 变了）才重新解码。
    if (oldWidget.dataUrl != widget.dataUrl) {
      _bytes = ChatMessage.decodeDataUrl(widget.dataUrl);
    }
  }

  @override
  Widget build(BuildContext context) {
    final Uint8List? bytes = _bytes;
    if (bytes == null) {
      // 解码失败就退回文本，不显示破图
      return Container(
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 9),
        decoration: BoxDecoration(
          color: Colors.white,
          borderRadius: BorderRadius.circular(8),
        ),
        child: const Text('[图片]',
            style: TextStyle(color: Color(0xFF1A1A1A), fontSize: 15)),
      );
    }
    return ClipRRect(
      borderRadius: BorderRadius.circular(8),
      child: Container(
        constraints: const BoxConstraints(maxWidth: 210, maxHeight: 260),
        color: const Color(0xFFEDEDED),
        child: Image.memory(bytes, fit: BoxFit.cover),
      ),
    );
  }
}

/// 对方（AI 或人类好友）头像：**跟着会话走**，只用本地文件（禁止网络加载）。
///
/// 2026-09-27 第七轮：以前这里读 AppConfig.avatarImage（全局那一张），
/// 于是所有会话共用一个头像；用户在某个聊天的设置里换了“对方头像”，
/// 气泡纹丝不动。现在优先用会话设置里存下来的本地文件；
/// 没有就画中性人形轮廓 —— 不拿内置图顶替（系统不替用户选脸）。
class _AiAvatar extends StatelessWidget {
  const _AiAvatar({this.path});

  final String? path;

  @override
  Widget build(BuildContext context) {
    // 2026-09-28 第八轮：传进来的 path 是**服务端路径**（`/uploads/...`），
    // 手机上 File(path) 永远不存在 → 永远画中性人形。必须先过 AvatarCache
    // 下载成本地文件。用户实测「换了头像气泡不变」的另一半原因就在这。
    final String? p = AvatarCache.instance.localPath(path);
    final bool hasFile = p != null && p.isNotEmpty && File(p).existsSync();
    return CircleAvatar(
      radius: 18,
      backgroundColor: const Color(0xFFE8EEF9),
      child: ClipOval(
        child: hasFile
            ? Image.file(
                File(p),
                width: 36,
                height: 36,
                fit: BoxFit.cover,
                errorBuilder: (_, _, _) => const Icon(
                  Icons.person,
                  size: 20,
                  color: Color(0xFFB9C2CF),
                ),
              )
            : const Icon(Icons.person, size: 20, color: Color(0xFFB9C2CF)),
      ),
    );
  }
}

/// 用户头像：用用户自己在「个人资料」里从相册选的那张（用户 2026-09-27 要求）。
///
/// 之前这里写死用内置头像的最后一张，用户改了头像气泡也不变；
/// 现在统一读 AppConfig.userAvatarImage。
/// 没设置过时**不拿内置头像顶替**，只画中性人形轮廓——用户头像必须由用户自己选。
class _UserAvatar extends StatelessWidget {
  const _UserAvatar();

  @override
  Widget build(BuildContext context) {
    final ImageProvider? img = AppConfig.instance.userAvatarImage;
    return CircleAvatar(
      radius: 18,
      backgroundColor: const Color(0xFFE0E0E0),
      child: ClipOval(
        child: img == null
            ? const Icon(Icons.person, size: 20, color: Color(0xFFB9C2CF))
            : Image(
                image: img,
                width: 36,
                height: 36,
                fit: BoxFit.cover,
                errorBuilder: (_, _, _) => const Icon(
                  Icons.person,
                  size: 20,
                  color: Color(0xFFB9C2CF),
                ),
              ),
      ),
    );
  }
}