// 聊天页：消息展示与 WebSocket 收发。
//
// 依据（QQ 界面）、3.1.4（时间戳）、3.4（群聊模式）。
//
// 2026-09-25 第二次 PRD 验证改动：
// 原来聊天页写死读 LocalQueue.conversation（单会话）。现在改成接收 conversationId，
// 这样消息列表里点哪个会话就进哪个 —— 群聊会话也走同一个页面。
// 群聊里每个 AI 有自己的头像和名字，气泡上要显示发言人名字（PRD 3.4.1
// “每个AI拥有独立的人格档案和内部状态”），单聊里则不需要（只有一个人）。
//
// 进入页面时会拉一次历史消息（服务端 /api/conversations/{id}/messages）：
// 新建的群聊没有历史，但单聊重装 App 后靠它把旧消息补回来。

import 'package:flutter/material.dart';

import '../models/conversation.dart';
import '../models/message.dart';
import '../services/api_client.dart';
import '../services/avatar_cache.dart';
import '../services/image_picker_service.dart';
import '../services/local_queue.dart';
import '../services/ws_client.dart';

import 'conversation_settings_page.dart';
import 'widgets/bubble.dart';
import 'widgets/input_bar.dart';

class ChatPage extends StatefulWidget {
  const ChatPage({super.key, required this.conversationId});

  final int conversationId;

  @override
  State<ChatPage> createState() => _ChatPageState();
}

class _ChatPageState extends State<ChatPage> {
  final LocalQueue _queue = LocalQueue.instance;
  final WsClient _ws = WsClient.instance;
  final ScrollController _scroll = ScrollController();

  @override
  void initState() {
    super.initState();
    _queue.enterChat(widget.conversationId); // 进入聊天框：未读清零（PRD 3.1.6）
    _queue.addListener(_onQueueChanged);
    _ws.addListener(_onWsChanged);
    // 2026-09-28 第八轮：头像要先从服务端下载到本地才画得出来。
    // 下载完 AvatarCache 会 notifyListeners，这里跟着重建，气泡头像就出现了。
    AvatarCache.instance.addListener(_onWsChanged);
    // 历史消息异步拉取，失败不影响已经能看的部分
    _queue.loadHistory(widget.conversationId);
    WidgetsBinding.instance.addPostFrameCallback((_) => _scrollToBottom());
  }

  @override
  void dispose() {
    _queue.removeListener(_onQueueChanged);
    _ws.removeListener(_onWsChanged);
    AvatarCache.instance.removeListener(_onWsChanged);
    _queue.leaveChat();
    _scroll.dispose();
    super.dispose();
  }

  void _onQueueChanged() {
    if (!mounted) return;
    // 2026-09-30 第十四轮：只在**消息列表真的变了**的时候才滚到底。
    // 原来任何一次 notify（收到消息、头像下载完、WS 状态变化）都排一次
    // post-frame 的 jumpTo，列表会平白无故往上顶一下——用户看到的就是「闪」。
    final List<ChatMessage> msgs = _queue.messagesOf(widget.conversationId);
    final String tail =
        msgs.isEmpty ? '' : '${msgs.last.msgId}#${msgs.last.segmentIndex}';
    final bool grew =
        msgs.length != _lastMessageCount || tail != _lastMessageTail;
    _lastMessageCount = msgs.length;
    _lastMessageTail = tail;
    setState(() {});
    if (grew) {
      WidgetsBinding.instance.addPostFrameCallback((_) => _scrollToBottom());
    }
  }

  /// 上一次 build 时消息列表的长度与最后一条的标识，用来判断「有没有新消息」。
  int _lastMessageCount = -1;
  String _lastMessageTail = '';

  void _onWsChanged() {
    if (mounted) setState(() {});
  }

  void _scrollToBottom() {
    if (!_scroll.hasClients) return;
    _scroll.jumpTo(_scroll.position.maxScrollExtent);
  }

  void _toast(String text) {
    if (!mounted) return;
    ScaffoldMessenger.of(context)
      ..hideCurrentSnackBar()
      ..showSnackBar(SnackBar(
        content: Text(text),
        duration: const Duration(seconds: 3),
      ));
  }

  bool _canSend() => _ws.isConnected;

  // ------------------------------------------------------------ 清空聊天记录

  /// 清空这个会话的聊天记录（用户 2026-10-04 要求）。
  ///
  /// 人类好友与 AI 好友**都支持**；对 AI 好友来说，服务端会连它的记忆一起清
  /// （短期窗口 / 中期摘要 / 长期事实 / 还没发出去的延迟消息），
  /// 所以清完之后它**不再记得**之前聊过什么 —— 这一点要写在确认框里，
  /// 让用户知道这不是"只是把屏幕擦干净"。
  Future<void> _clearHistory() async {
    // 会话对象与标题都只在 build/其他方法里取（它们是局部变量），这里自己取一次。
    final Conversation? c = _queue.conversationById(widget.conversationId);
    final bool isAi = c != null && !c.isHuman;
    final String name = c?.title ?? '对方';
    final bool? ok = await showDialog<bool>(
      context: context,
      builder: (BuildContext ctx) => AlertDialog(
        title: const Text('清空聊天记录'),
        content: Text(
          isAi
              ? '将删除你和「$name」的全部聊天记录，且它**不会再记得**之前聊过的内容'
                  '（记忆一起清空）。\n\n这个操作不能撤销。'
              : '将删除你和「$name」的全部聊天记录。\n\n这个操作不能撤销。',
        ),
        actions: <Widget>[
          TextButton(
            onPressed: () => Navigator.pop(ctx, false),
            child: const Text('取消'),
          ),
          TextButton(
            onPressed: () => Navigator.pop(ctx, true),
            child: const Text('清空', style: TextStyle(color: Color(0xFFE64340))),
          ),
        ],
      ),
    );
    if (ok != true) return;

    try {
      final Map<String, dynamic> res =
          await ApiClient.instance.clearConversation(widget.conversationId);
      // 本机这份缓存也要清：不然屏幕上还留着旧消息，排队中的旧消息还会继续"打字"。
      _queue.clearConversation(widget.conversationId);
      final dynamic removed = res['removed'];
      final int count = removed is Map && removed['messages'] is num
          ? (removed['messages'] as num).toInt()
          : 0;
      _toast(count > 0 ? '已清空 $count 条聊天记录' : '聊天记录已清空');
    } catch (e) {
      _toast('清空失败：$e');
    }
  }

  // ------------------------------------------------------------ 聊天设置

  /// 右上角设置：改这一个聊天对象的头像/名称/API/人格数据。
  ///
  /// 单聊直接进；群聊有多个 AI，先让用户选改哪一个（PRD 3.4.1：
  /// 每个 AI 拥有独立的人格档案和内部状态，所以必须能分别设置）。
  ///
  /// 人类好友会话不进这一页：用户把这一页限定为五项（对方的头像、名称、
  /// API接口、APIKEY、人格对话数据），而人类好友既没有 API 也没有人格档案，
  /// 进去只会看到一堆填不了的框。人类会话的删除入口也在这里，先留一个提示。
  Future<void> _openConversationSettings() async {
    final Conversation? conv = _queue.conversationById(widget.conversationId);
    if (conv != null && conv.isHuman) {
      _toast('这是真人好友，没有 API 与人格设置');
      return;
    }
    final List<int> members = conv?.participants ?? const <int>[];
    if (members.isEmpty) {
      _toast('这个会话还没有 AI 成员');
      return;
    }

    int target = members.first;
    if (members.length > 1) {
      final int? picked = await _pickMember(members);
      if (picked == null) return;
      target = picked;
    }

    if (!mounted) return;
    // 返回 'saved' = 只是改了设置；返回 'deleted' = 这个好友被删了。
    // 用户 2026-09-27 第六轮要求「随时可以删除这个 AI 聊天好友」，
    // 删完必须把聊天页一起关掉——会话已经不存在了，留着会一直发不出去。
    final String? result = await Navigator.of(context).push<String>(
      MaterialPageRoute<String>(
        builder: (_) => ConversationSettingsPage(
          aiProfileId: target,
          conversationId: widget.conversationId,
        ),
      ),
    );
    await _queue.refreshConversations();
    if (!mounted) return;
    if (result == 'deleted') {
      Navigator.of(context).pop();
      return;
    }
    setState(() {});
  }

  /// 群聊里选一个成员来设置。名字从服务端 AI 档案列表取。
  Future<int?> _pickMember(List<int> members) async {
    Map<int, String> names = <int, String>{};
    try {
      final List<Map<String, dynamic>> list =
          await ApiClient.instance.listAiProfiles();
      for (final Map<String, dynamic> p in list) {
        final int id = (p['id'] as num?)?.toInt() ?? 0;
        if (id != 0) names[id] = p['name']?.toString() ?? 'AI';
      }
    } catch (_) {
      // 取不到名字不影响设置，退化成“AI 1 / AI 2”
    }
    if (!mounted) return null;
    return showModalBottomSheet<int>(
      context: context,
      builder: (BuildContext ctx) => SafeArea(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: <Widget>[
            const Padding(
              padding: EdgeInsets.symmetric(vertical: 12),
              child: Text('设置哪一个 AI',
                  style: TextStyle(fontSize: 14, color: Color(0xFF666666))),
            ),
            for (final int id in members)
              ListTile(
                leading: const Icon(Icons.smart_toy_outlined),
                title: Text(names[id] ?? 'AI $id'),
                onTap: () => Navigator.of(ctx).pop(id),
              ),
          ],
        ),
      ),
    );
  }

  void _handleSendText(String text) {
    if (!_ws.isConnected) {
      // 2026-09-27 第七轮：设置页里那个按钮已经不叫「保存并重连」了，
      // 提示文案要跟着改，否则用户按图索骥找不到按钮。
      _toast('未连接服务端，请到设置页确认服务器地址并点“重连”');
      return;
    }
    final String cid = WsClient.newClientMsgId();
    _queue.addLocalUserMessage(
      clientMsgId: cid,
      text: text,
      conversationId: widget.conversationId,
    );
    _ws.sendUserMessage(
      conversationId: widget.conversationId,
      clientMsgId: cid,
      content: text,
    );
  }

  void _handleSendImage(PickedImage image) {
    if (!_ws.isConnected) {
      _toast('未连接服务端，图片没有发出去');
      return;
    }
    final String cid = WsClient.newClientMsgId();
    _queue.addLocalUserMessage(
      clientMsgId: cid,
      text: image.dataUrl,
      contentType: 'image',
      conversationId: widget.conversationId,
    );
    _ws.sendUserMessage(
      conversationId: widget.conversationId,
      clientMsgId: cid,
      content: image.dataUrl,
      contentType: 'image',
    );
  }

  @override
  Widget build(BuildContext context) {
    final List<ChatMessage> msgs = _queue.messagesOf(widget.conversationId);
    final Conversation? conv = _queue.conversationById(widget.conversationId);
    // 人类好友会话里「对方」的 users.id：气泡靠它判断哪条是对方发的
    // （2026-09-30 第十四轮）。
    final int peerUserId = conv?.peerUserId ?? 0;
    final bool isGroup = conv?.isGroup ?? false;
    // 标题只信服务端给的会话标题（单聊=对方昵称，群聊=群名）。
    // 2026-09-27 第五轮：原来回退到 AppConfig.aiName（默认「小真」），
    // 会在会话还没加载出来时闪一下「小真」——用户明确要求这个东西永久消失。
    //
    // 2026-09-30 第十五轮（用户报告「给模型改名之后聊天界面不会实时更新」）：
    // 这里原来读 conv.title —— 那是**建会话时抄下来的** AI 名字，
    // 而改名接口只动 ai_profiles.name，于是标题永远停在旧名字上。
    // 改用 peerDisplayName：单聊取 ai_name（服务端每次现读 ai_profiles），
    // 群聊取群名、人类好友会话取对方昵称，改完名立刻就是新的。
    final String title = conv?.peerDisplayName ?? '聊天';

    return Scaffold(
      backgroundColor: const Color(0xFFF2F2F2),
      appBar: AppBar(
        backgroundColor: const Color(0xFFF7F7F7),
        elevation: 0.5,
        foregroundColor: Colors.black87,
        title: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: <Widget>[
            Text(title, style: const TextStyle(fontSize: 16)),
            // 副标题：群聊报成员数；单聊显示亲密度与在线状态。
            if (isGroup)
              Text(
                '${conv?.participants.length ?? 0} 个 AI',
                style: const TextStyle(fontSize: 11, color: Color(0xFF999999)),
              )
            else if (conv != null && conv.isHuman)
              Text(
                conv.peerOnline ? '在线' : '离线',
                style: TextStyle(
                  fontSize: 11,
                  color: conv.peerOnline
                      ? const Color(0xFF07C160)
                      : const Color(0xFF999999),
                ),
              )
            else if (conv != null && conv.intimacyPercent > 0)
              Text(
                '亲密度 ${conv.intimacyPercent}%'
                '${conv.intimacyLabel.isEmpty ? '' : ' · ${conv.intimacyLabel}'}',
                style: const TextStyle(fontSize: 11, color: Color(0xFFD6467A)),
              ),
          ],
        ),
        actions: <Widget>[
          // 右上角设置：改的是「这一个聊天对象」（头像/名称/API/人格数据），
          // 不是全局设置。用户 2026-09-27 明确要求。
          IconButton(
            tooltip: '聊天设置',
            icon: const Icon(Icons.settings_outlined, size: 20),
            onPressed: _openConversationSettings,
          ),
          PopupMenuButton<String>(
            tooltip: '更多',
            icon: const Icon(Icons.more_vert, size: 20),
            onSelected: (String value) {
              if (value == 'clear') _clearHistory();
            },
            itemBuilder: (BuildContext ctx) => <PopupMenuEntry<String>>[
              const PopupMenuItem<String>(
                value: 'clear',
                child: Text('清空聊天记录', style: TextStyle(color: Color(0xFFE64340))),
              ),
            ],
          ),
        ],
      ),
      body: Column(
        children: <Widget>[
          _ConnectionBar(ws: _ws),
          Expanded(
            child: msgs.isEmpty
                ? const Center(
                    child: Text('还没有消息',
                        style: TextStyle(color: Color(0xFF999999))),
                  )
                : ListView.builder(
                    controller: _scroll,
                    padding: const EdgeInsets.only(top: 8, bottom: 8),
                    itemCount: msgs.length,
                    itemBuilder: (BuildContext context, int index) {
                      final ChatMessage m = msgs[index];
                      return Bubble(
                        // 2026-09-30 第十四轮：带上稳定的 key。
                        // loadHistory 是「先清空再重建」，没有 key 时 Flutter 按
                        // 下标复用元素（第 0 个位置原来是图片、现在换成文字），
                        // 会看到一帧错内容再纠正过来 —— 也是一种「闪」。
                        key: ValueKey<String>(
                            '${m.msgId}#${m.segmentIndex}'),
                        message: m,
                        // 真人好友会话：这条是不是「我发的」要靠它判，
                        // 否则对方的消息会画到右边（用户实测反馈）。
                        peerUserId: peerUserId,
                        showTime: _shouldShowTime(msgs, index),
                        showSenderName: isGroup,
                        // 头像跟着会话走：用户在聊天设置里换的“对方头像”
                        // 必须立刻出现在气泡上（第七轮修正）。
                        aiAvatarPath: conv?.avatarUrl,
                      );
                    },
                  ),
          ),
          InputBar(
            onSendText: _handleSendText,
            onSendImage: _handleSendImage,
            canSend: _canSend,
          ),
        ],
      ),
    );
  }

  /// 时间戳显示逻辑（PRD 3.1.4）：同一分钟内不重复显示，换分钟或首条时显示。
  bool _shouldShowTime(List<ChatMessage> msgs, int index) {
    if (index == 0) return true;
    final DateTime prev = msgs[index - 1].timestamp;
    final DateTime cur = msgs[index].timestamp;
    return prev.year != cur.year ||
        prev.month != cur.month ||
        prev.day != cur.day ||
        prev.hour != cur.hour ||
        prev.minute != cur.minute;
  }
}

/// 聊天页顶部的连接状态条：连上时不占地方，断开时把原因写清楚。
class _ConnectionBar extends StatelessWidget {
  const _ConnectionBar({required this.ws});

  final WsClient ws;

  @override
  Widget build(BuildContext context) {
    if (ws.isConnected) return const SizedBox.shrink();

    final bool connecting = ws.isConnecting;
    final String detail = ws.lastErrorDetail ??
        (connecting ? '正在连接 ${ws.currentUrl} …' : '未连接');

    return Container(
      width: double.infinity,
      color: connecting ? const Color(0xFFFFF6E5) : const Color(0xFFFFECEC),
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          Icon(
            connecting ? Icons.sync : Icons.error_outline,
            size: 16,
            color: connecting ? const Color(0xFFB8860B) : const Color(0xFFDD3333),
          ),
          const SizedBox(width: 8),
          Expanded(
            child: Text(
              detail,
              style: TextStyle(
                fontSize: 12,
                color: connecting ? const Color(0xFF8A6100) : const Color(0xFF9B1C1C),
              ),
            ),
          ),
        ],
      ),
    );
  }
}
