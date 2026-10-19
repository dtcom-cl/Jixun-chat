// 会话数据结构：供消息列表、未读标记与群聊界面使用。
//
// 字段对应 conversations 表，以及服务端 /api/conversations
// 返回的 JSON（src/server/main.py 的 _conversation_to_json）。
//
// PRD 3.4.1：一个会话可能是单聊（一个 AI），也可能是群聊（多个 AI）。
// 单聊时 [title] 由会话设置页里的名称决定；群聊时是用户创建时起的群名。
//
// 2026-09-27 第七轮新增两类会话与两个展示字段：
//   · type == 'human'：人类好友会话（对方是真人，走服务器直投，不经 AI）；
//   · [avatarUrl]：这个会话里“对方”的头像（AI 读 ai_profiles，人类读 users）。
//     以前气泡头像读的是全局内置头像，用户在会话设置里换了头像也不变。
//   · [intimacyPercent] / [intimacyLabel]：亲密度百分值，显示在聊天设置旁边
//     （PRD 3.2.2 关系随对话累积）。

import 'message.dart';

class Conversation {
  Conversation({
    required this.id,
    required this.title,
    this.type = 'single',
    this.participants = const <int>[],
    this.aiName = '',
    this.avatarUrl,
    this.hasBackground = false,
    this.unreadCount = 0,
    this.lastMessageAt,
    this.lastMessage,
    this.preview = '',
    this.peerUserId,
    this.peerName = '',
    this.peerOnline = false,
    this.intimacyPercent = 0,
    this.intimacyLabel = '',
  });

  final int id;
  String title;

  /// single / group / human。
  String type;

  /// 参与者 AI profile ID 列表（群聊时多个）。人类会话为空。
  List<int> participants;

  /// 单聊时该会话绑定的 AI 昵称（群聊 / 人类会话为空）。
  String aiName;

  /// 对方头像的本地文件路径（会话设置里选的那张）。没有则为 null。
  String? avatarUrl;

  /// 是否上传过群聊上下文背景文档（PRD 3.4.1）。
  bool hasBackground;

  int unreadCount;
  DateTime? lastMessageAt;
  ChatMessage? lastMessage;

  /// 服务端给的最后一条消息摘要，本地还没有 [lastMessage] 时用它兜底。
  String preview;

  // ---------------- 人类好友（第七轮） ----------------

  /// 人类好友会话里对方的 users.id（非人类会话为 null）。
  int? peerUserId;

  /// 人类好友的昵称。
  String peerName;

  /// 人类好友是否在线（服务端按 WebSocket 连接状态判定）。
  bool peerOnline;

  // ---------------- 亲密度（第七轮） ----------------

  /// 亲密度百分值 0~100（PRD 3.2.2）。
  int intimacyPercent;

  /// 一句人话描述（比如「挺熟的」），空串表示不显示。
  String intimacyLabel;

  bool get isGroup => type == 'group';

  /// 人类好友会话：发出去的消息直接投给对方，不经 AI。
  bool get isHuman => type == 'human';

  /// 能不能进「聊天设置」。群聊没有单一对方，人类好友没有 AI 档案，
  /// 两者都不该出现那五项设置（用户明确限定那是「对方是 AI」时才有）。
  bool get hasSettings => !isGroup && !isHuman;

  /// 会话里“对方”的显示名。
  String get peerDisplayName {
    if (isHuman) return peerName.isEmpty ? title : peerName;
    if (isGroup) return title;
    return aiName.isEmpty ? title : aiName;
  }

  String get subtitle {
    final ChatMessage? m = lastMessage;
    if (m == null) return preview.isEmpty ? '还没有聊天记录' : preview;
    // 图片消息的 text 是 data URL，直接显示会刷屏，这里给个占位文本。
    if (m.isImage) return '[图片]';
    return m.text;
  }

  /// 服务端 /api/conversations 的一条记录 -> Conversation。
  static Conversation fromJson(Map<String, dynamic> json) {
    final dynamic rawParticipants = json['participants'];
    final List<int> ids = <int>[];
    if (rawParticipants is List) {
      for (final dynamic p in rawParticipants) {
        if (p is num) ids.add(p.toInt());
      }
    }
    final String type = json['type']?.toString() ?? 'single';
    final String aiName = json['ai_name']?.toString() ?? '';
    final String peerName = json['peer_name']?.toString() ?? '';
    // 头像：AI 会话读 ai_avatar，人类会话读 peer_avatar。
    final String? avatar = type == 'human'
        ? _nonEmpty(json['peer_avatar'])
        : _nonEmpty(json['ai_avatar']);
    return Conversation(
      id: (json['id'] as num?)?.toInt() ?? 0,
      title: json['title']?.toString() ?? '会话',
      type: type,
      participants: ids,
      aiName: aiName,
      avatarUrl: avatar,
      hasBackground: json['has_background'] == true,
      unreadCount: (json['unread_count'] as num?)?.toInt() ?? 0,
      preview: json['last_message_preview']?.toString() ?? '',
      peerUserId: (json['peer_user_id'] as num?)?.toInt(),
      peerName: peerName,
      peerOnline: json['peer_online'] == true,
      intimacyPercent: (json['intimacy_percent'] as num?)?.toInt() ?? 0,
      intimacyLabel: json['intimacy_label']?.toString() ?? '',
    );
  }

  static String? _nonEmpty(dynamic v) {
    final String s = v?.toString().trim() ?? '';
    return s.isEmpty ? null : s;
  }
}
