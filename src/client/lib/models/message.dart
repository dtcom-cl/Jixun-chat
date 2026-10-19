// 消息数据结构：供本地队列、多段呈现与时间戳使用。
//
// 字段与 message 一一对应，不额外增加协议字段。
//

import 'dart:convert';
import 'dart:typed_data';

enum SenderType { user, ai, system }

SenderType senderTypeFrom(String? raw) {
  switch (raw) {
    case 'user':
      return SenderType.user;
    case 'ai':
      return SenderType.ai;
    default:
      return SenderType.system;
  }
}

/// 一条待展示的消息气泡。
///
/// 服务端推来的 [content] 是数组（多段消息），客户端为每一段生成一个
/// ChatMessage，由打字机引擎按 1-3 秒间隔依次展示。
class ChatMessage {
  final String msgId;
  final int conversationId;
  final SenderType senderType;
  final int senderId;
  final String senderName;
  final String? senderAvatar;
  final String text;
  final String contentType;
  final DateTime timestamp;
  final bool isProactive;
  final bool isDoubleText;
  final int segmentIndex; // 该消息在 content 数组中的下标
  final int segmentCount; // content 数组长度

  /// 这条消息是不是「对方（另一个真人）」发来的。
  ///
  /// 2026-09-30 第十四轮：真人好友会话里**双方都是 sender_type = user**
  /// （服务端把消息复制一份落到对方会话里，sender_id 仍是发送者），
  /// 所以「sender_type == user」根本不能拿来判断「这是我发的」——
  /// 用户实测的「对面发的消息变成自己发的」就是这个。
  /// 服务端投递给对方时会带 from_peer = true
  /// （human_chat.deliver_human_message），这里照收。
  final bool fromPeer;

  ChatMessage({
    required this.msgId,
    required this.conversationId,
    required this.senderType,
    required this.senderId,
    required this.senderName,
    required this.senderAvatar,
    required this.text,
    required this.contentType,
    required this.timestamp,
    this.isProactive = false,
    this.isDoubleText = false,
    this.fromPeer = false,
    this.segmentIndex = 0,
    this.segmentCount = 1,
  });

  bool get isFirstSegment => segmentIndex == 0;

  /// 只是「发送者类型是 user」，**不代表这条是我发的**。
  ///
  /// 要判断归属（右对齐蓝气泡 / 我的头像）必须用 [isMineFor]。
  bool get isUser => senderType == SenderType.user;

  /// 这条消息该不该画成「我自己发的」。
  ///
  /// [peerUserId] = 当前会话里对方的 users.id（人类好友会话才有，其它会话传 0）。
  /// 判定顺序：
  /// 1. 服务端明确标了 from_peer -> 一定是对方发来的；
  /// 2. 不是 user 类型（ai / system）-> 不是；
  /// 3. sender_id == 0 是本地乐观上屏的那条 -> 是我发的；
  /// 4. sender_id 等于对方 id -> 对方发来的；
  /// 5. 其余（含 AI 单聊里我自己的历史消息，sender_id 是我的 users.id）-> 我发的。
  bool isMineFor(int peerUserId) {
    if (fromPeer) return false;
    if (senderType != SenderType.user) return false;
    if (senderId == 0) return true;
    if (peerUserId != 0 && senderId == peerUserId) return false;
    return true;
  }

  /// 是不是一张图片（content 为 data URL）。
  bool get isImage => contentType == 'image' && looksLikeDataUrl(text);

  /// 本地乐观插入的用户消息（还带 client_msg_id，服务端回包前先显示）。
  ChatMessage.localUser({
    required this.conversationId,
    required String clientMsgId,
    required this.text,
    required this.timestamp,
    this.contentType = 'text',
  })  : msgId = clientMsgId,
        senderType = SenderType.user,
        senderId = 0,
        senderName = '我',
        senderAvatar = null,
        isProactive = false,
        isDoubleText = false,
        fromPeer = false,
        segmentIndex = 0,
        segmentCount = 1;

  /// 把 的 message JSON 拆成若干段 ChatMessage。
  static List<ChatMessage> fromWsJson(Map<String, dynamic> json) {
    final dynamic rawContent = json['content'];
    late final List<String> segments;
    if (rawContent is List) {
      segments = rawContent.map((dynamic e) => e.toString()).toList();
    } else if (rawContent is String) {
      // 容错：服务端理论上一定给数组，这里兜底当单段处理
      try {
        final dynamic decoded = jsonDecode(rawContent);
        segments = decoded is List
            ? decoded.map((dynamic e) => e.toString()).toList()
            : <String>[rawContent];
      } catch (_) {
        segments = <String>[rawContent];
      }
    } else {
      segments = <String>[];
    }

    final DateTime ts =
        DateTime.tryParse(json['timestamp']?.toString() ?? '')?.toLocal() ??
            DateTime.now();
    final String msgId = json['msg_id']?.toString() ?? '';
    final int conversationId = (json['conversation_id'] as num?)?.toInt() ?? 1;
    final String contentType = json['content_type']?.toString() ?? 'text';

    return List<ChatMessage>.generate(segments.length, (int i) {
      return ChatMessage(
        msgId: msgId,
        conversationId: conversationId,
        senderType: senderTypeFrom(json['sender_type']?.toString()),
        senderId: (json['sender_id'] as num?)?.toInt() ?? 0,
        senderName: json['sender_name']?.toString() ?? 'AI',
        senderAvatar: json['sender_avatar']?.toString(),
        text: segments[i],
        contentType: contentType,
        timestamp: ts,
        isProactive: json['is_proactive'] == true,
        isDoubleText: json['is_double_text'] == true,
        fromPeer: json['from_peer'] == true,
        segmentIndex: i,
        segmentCount: segments.length,
      );
    });
  }

  // ------------------------------------------------------------ 图片工具

  /// 文本是不是 data URL（图片消息的 content 格式）。
  static bool looksLikeDataUrl(String s) => s.startsWith('data:image/');

  /// 把 data URL 解成字节，交给 Image.memory 渲染；失败返回 null。
  static Uint8List? decodeDataUrl(String dataUrl) {
    final int comma = dataUrl.indexOf(',');
    if (comma < 0) return null;
    try {
      return base64Decode(dataUrl.substring(comma + 1));
    } catch (_) {
      return null;
    }
  }
}
