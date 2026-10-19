// 本地消息队列、会话列表与未读管理。
//
// 职责：
// - 缓存服务端推送的消息，按顺序交给打字机引擎展示；
// - 管理每个会话的未读数与小红点（PRD 3.1.6）；
// - 记录最后收到的 msg_id，供断线重连时 resume 使用。
//
// 2026-09-25 第二次 PRD 验证改动：
// 早期实现只有单一会话（conversation_id = 1），因此原本是单会话模型，
// 整个类只有一个 conversation 字段、一份消息列表。
// 但 PRD 3.4.1 要求“用户可在设置中创建群聊、多个 AI 进入同一个群聊”，
// 消息列表（PRD 3.1.6）也必须能放下多个会话 —— 单会话模型撑不住，因此改成：
//   · conversations：会话列表，由服务端 /api/conversations 拉取；
//   · 每个会话各自一份消息列表 + 去重集合 + 打字机队列（互不干扰）；
//   · viewingConversationId：当前正在看的会话，决定未读怎么加；
//   · lastMsgId 仍是全局的 —— resume 的语义就是“这个用户的所有会话”，
//     WS-001 3.5 的补发查询本身也是按 user_id 跨会话查的。

import 'dart:async';

import 'package:flutter/foundation.dart';

import '../config/app_config.dart';
import '../models/conversation.dart';
import '../models/message.dart';
import '../utils/typewriter.dart';
import 'api_client.dart';
import 'local_store.dart';

class LocalQueue extends ChangeNotifier {
  LocalQueue._();

  static final LocalQueue instance = LocalQueue._();

  // ------------------------------------------------------------ 会话

  final List<Conversation> _conversations = <Conversation>[];

  /// 当前正在查看的会话 id；0 表示不在聊天页。
  int viewingConversationId = 0;

  /// 兜底会话：只在「一个会话都没有」时被 [conversation] getter 返回，
  /// **不会**被塞进 _conversations，所以消息列表页能正确显示空状态
  /// （用户 2026-09-27 要求：像 QQ 一样首次进入什么都没有，自己加好友/建群）。
  ///
  final Conversation _placeholder = Conversation(id: 0, title: '', type: '');

  List<Conversation> get conversations =>
      List<Conversation>.unmodifiable(_conversations);

  Conversation? conversationById(int id) {
    for (final Conversation c in _conversations) {
      if (c.id == id) return c;
    }
    return null;
  }

  /// 当前会话（设置页 / 聊天页用）。不在聊天页时退回第一个会话。
  Conversation get conversation {
    if (viewingConversationId != 0) {
      final Conversation? c = conversationById(viewingConversationId);
      if (c != null) return c;
    }
    if (_conversations.isNotEmpty) return _conversations.first;
    return _placeholder;
  }

  // ------------------------------------------------------------ 消息

  final Map<int, List<ChatMessage>> _messages = <int, List<ChatMessage>>{};
  final Map<int, Set<String>> _seen = <int, Set<String>>{};
  final Map<int, Typewriter<ChatMessage>> _writers =
      <int, Typewriter<ChatMessage>>{};

  /// 最后收到的消息ID，resume 时上报（WS-001 3.5）。全局，跨会话。
  ///
  /// 2026-09-28 第八轮：**落盘**。
  /// 原来这只是个内存变量，杀掉 App 就没了 —— 冷启动重连时 resume 带的是空串，
  /// 于是服务端把「该用户全部消息」重发一遍（历史被重复插入，顺序全乱），
  /// 而真正需要的「离线期间到期的那几条延迟消息」反而认不出来。
  /// 现在跟着 LocalStore 一起持久化，冷启动也能接上。
  String _lastMsgId = '';

  /// 连服务端时手上那个锚点是不是**真的**（而不是空串兜底）。
  ///
  bool _resumeAnchorKnown = true;

  String get lastMsgId => _lastMsgId;

  set lastMsgId(String value) {
    if (value.isEmpty || value == _lastMsgId) return;
    _lastMsgId = value;
    // 不 await：落盘是后台动作，不能挡住消息上屏。
    unawaited(_persistLastMsgId());
  }

  /// 把 resume 锚点**往前推**（只前不后）。
  ///
  /// 2026-09-30 第十六轮：原来 loadHistory 直接 `lastMsgId = 该会话最新的一条`，
  /// 那可能比别的会话里已经收到的消息更旧 —— 锚点一退，下次 resume 就把已经看过
  /// 的消息又补发一遍（用户看到的就是「重复的老消息」）。现在比时间戳，只进不退。
  void _advanceAnchor(String msgId) {
    if (msgId.isEmpty) return;
    if (_stampOf(msgId) >= _stampOf(_lastMsgId)) {
      lastMsgId = msgId;
    }
  }

  /// 从 msg_id 里取毫秒时间戳（服务端格式 `msg_1790694491320_ab12`）。
  /// 取不到返回 0 —— 那样就退回「照单全收」的老行为，不会把锚点卡死。
  static int _stampOf(String msgId) {
    final Match? hit = RegExp(r'_(\d{9,})_').firstMatch(msgId);
    if (hit == null) return 0;
    return int.tryParse(hit.group(1)!) ?? 0;
  }

  Future<void> _persistLastMsgId() async {
    try {
      final Map<String, dynamic> data = await LocalStore.read();
      data['last_msg_id'] = _lastMsgId;
      // 跟着账号走：换账号登录时不能拿上一个账号的锚点去 resume。
      data['last_msg_id_owner'] = AppConfig.instance.accountId;
      await LocalStore.write(data);
    } catch (_) {
      // 落盘失败只影响冷启动补发，不影响当前会话。
    }
  }

  /// 冷启动时把上次的锚点读回来（main.dart 在登录后调一次）。
  Future<void> restoreLastMsgId() async {
    try {
      final Map<String, dynamic> data = await LocalStore.read();
      final String owner = data['last_msg_id_owner']?.toString() ?? '';
      if (owner != AppConfig.instance.accountId) {
        // 换了账号：锚点必须丢掉，否则新账号会拿旧账号的消息 ID 去查补发。
        _lastMsgId = '';
        _resumeAnchorKnown = false;
        return;
      }
      _lastMsgId = data['last_msg_id']?.toString() ?? '';
    } catch (_) {
      _lastMsgId = '';
    }
    // 有真锚点才敢把补发当「新消息」；空锚点意味着服务端会发全部历史过来。
    _resumeAnchorKnown = _lastMsgId.isNotEmpty;
  }

  String? lastErrorText;

  List<ChatMessage> messagesOf(int conversationId) =>
      List<ChatMessage>.unmodifiable(
          _messages[conversationId] ?? const <ChatMessage>[]);

  List<ChatMessage> get messages => messagesOf(viewingConversationId);

  int get pendingCount {
    int n = 0;
    for (final Typewriter<ChatMessage> w in _writers.values) {
      n += w.pendingCount;
    }
    return n;
  }

  Typewriter<ChatMessage> _writerOf(int conversationId) {
    return _writers.putIfAbsent(
      conversationId,
      () => Typewriter<ChatMessage>(
        onEmit: (ChatMessage m) async => _emit(conversationId, m),
        textOf: (ChatMessage m) => m.text,
      ),
    );
  }

  Future<void> _emit(int conversationId, ChatMessage m) async {
    (_messages[conversationId] ??= <ChatMessage>[]).add(m);
    final Conversation? c = conversationById(conversationId);
    if (c != null) {
      c.lastMessage = m;
      c.lastMessageAt = m.timestamp;
    }
    _sortConversations();
    notifyListeners();
  }

  /// 会话列表按最后一条消息时间倒序（PRD 3.1.6）。
  void _sortConversations() {
    _conversations.sort((Conversation a, Conversation b) {
      final DateTime? ta = a.lastMessageAt;
      final DateTime? tb = b.lastMessageAt;
      if (ta == null && tb == null) return a.id.compareTo(b.id);
      if (ta == null) return 1;
      if (tb == null) return -1;
      return tb.compareTo(ta);
    });
  }

  // ------------------------------------------------------------ 会话列表（REST）

  /// 用服务端 /api/conversations 的结果刷新会话列表。
  ///
  /// 2026-09-28 第八轮改成**以服务端为准的全量同步**。
  /// 原来这里是「只增不删」：服务端已经没有的会话，本地还挂着。
  /// 用户实测到的「凭空多出一个小张」就是这么来的 —— 从账号 0001 退出、
  /// 登进 0000，上一个账号的会话还在列表里，点进去服务端 404，
  /// 界面显示「AI 档案不存在」。
  ///
  /// 只保留本地已经累积的**消息与未读**：服务端返回的是权威的会话清单，
  /// 但本地正在打字机里排队的消息不能因为一次刷新就丢掉。
  void applyConversations(List<Conversation> remote) {
    final Set<int> remoteIds = <int>{for (final Conversation r in remote) r.id};
    // 服务端没有的一律删掉（含它的消息、去重集合、打字机队列）。
    final List<int> gone = <int>[
      for (final Conversation c in _conversations)
        if (!remoteIds.contains(c.id)) c.id,
    ];
    for (final int id in gone) {
      _conversations.removeWhere((Conversation c) => c.id == id);
      _messages.remove(id);
      _seen.remove(id);
      _writers.remove(id);
    }

    for (final Conversation r in remote) {
      final Conversation? local = conversationById(r.id);
      if (local == null) {
        _conversations.add(r);
        continue;
      }
      local.title = r.title;
      local.type = r.type;
      local.participants = r.participants;
      local.aiName = r.aiName;
      // 第七轮：头像、人类好友、亲密度都要跟着服务端刷新。
      // 特别是 avatarUrl —— 用户在会话设置里换了头像，列表和气泡必须立刻变，
      // 这是上一版「改了头像没反应」的直接原因。
      local.avatarUrl = r.avatarUrl;
      local.peerUserId = r.peerUserId;
      local.peerName = r.peerName;
      local.peerOnline = r.peerOnline;
      local.intimacyPercent = r.intimacyPercent;
      local.intimacyLabel = r.intimacyLabel;
      local.hasBackground = r.hasBackground;
      local.preview = r.preview;
      // 本地还没有消息时用服务端的未读数（冷启动场景）
      if (local.lastMessageAt == null) {
        local.unreadCount = r.unreadCount;
      }
    }
    // 这里**不**再补占位会话：空列表就是空列表（首次启动、还没加好友）。
    _sortConversations();
    notifyListeners();
  }

  /// 清空所有本地缓存（退出登录 / 被挤下线时调用）。
  ///
  /// 2026-09-28 第八轮新增。原来退出登录只清了凭证和用户头像，
  /// 会话列表、消息、去重集合、resume 锚点全部原样留着 ——
  /// 换账号登录后看到的就是上一个账号的聊天记录，点进去必然 404。
  /// 这是「串号」的另一半原因（另一半是 applyConversations 只增不删）。
  void clearAll() {
    _conversations.clear();
    _messages.clear();
    _seen.clear();
    for (final Typewriter<ChatMessage> w in _writers.values) {
      w.clear();
    }
    _writers.clear();
    viewingConversationId = 0;
    _lastMsgId = '';
    // 退出登录/被挤下线：本地没有锚点了，下次连上时服务端会把全部历史发过来，
    // 那一批只能用来对齐锚点（见 _resumeAnchorKnown 的注释）。
    _resumeAnchorKnown = false;
    lastErrorText = null;
    unawaited(_persistLastMsgId());
    notifyListeners();
  }

  /// 拉取会话列表。失败只记录文本，不打断界面（断网时仍能看本地消息）。
  Future<void> refreshConversations() async {
    try {
      final List<Map<String, dynamic>> raw =
          await ApiClient.instance.listConversations();
      applyConversations(raw.map(Conversation.fromJson).toList());
      lastErrorText = null;
    } catch (e) {
      lastErrorText = e.toString();
      // 拉不到就保持现状（可能本来就是空的）；不要伪造会话，
      // 否则用户会以为“好友已经在列表里了”，点进去却发不出消息。
      notifyListeners();
    }
  }

  // ------------------------------------------------------------ 历史消息（REST）

  /// 进入聊天页时拉取历史消息（服务端 /api/conversations/{id}/messages）。
  ///
  /// 历史消息直接上屏，不走打字机 —— 打字机只模拟“刚刚收到”的新消息。
  ///
  /// 2026-09-28 第八轮：**先清空再重建**，以服务端历史为唯一真相。
  /// 原来是 `store.insertAll(0, fresh)` 且不清空内存里的消息，
  /// 重进聊天页时整段历史被插到已有消息前面，顺序就交错了 ——
  /// 用户看到的「退出后聊天记录格式错乱」正是这个。
  /// 而且去重集合 `_seen` 是按 `msgId#segmentIndex` 记的，服务端重启后
  /// msg_id 变了，同一句话会被当成新消息再插一次。
  /// 清空后重建两个问题一起消失：顺序由服务端决定，去重集合与之一致。
  Future<void> loadHistory(int conversationId, {int limit = 50}) async {
    try {
      final List<Map<String, dynamic>> raw =
          await ApiClient.instance.listMessages(conversationId, limit: limit);
      final List<ChatMessage> history = <ChatMessage>[];
      for (final Map<String, dynamic> item in raw) {
        history.addAll(ChatMessage.fromWsJson(item));
      }

      // 服务端是唯一真相：先把这个会话的本地状态整个丢掉。
      // 正在打字机里排队的消息也一并清掉 —— 它们要么已经在服务端的历史里
      // （那就不该重复出现），要么是刚推送还没落库（下一次 WS 会补上）。
      (_writers[conversationId])?.clear();
      _messages[conversationId] = <ChatMessage>[];
      final Set<String> seen = <String>{};
      _seen[conversationId] = seen;

      final List<ChatMessage> store = _messages[conversationId]!;
      for (final ChatMessage m in history) {
        if (seen.add('${m.msgId}#${m.segmentIndex}')) store.add(m);
      }
      if (store.isNotEmpty) {
        final Conversation? c = conversationById(conversationId);
        if (c != null) {
          c.lastMessage = store.last;
          c.lastMessageAt = store.last.timestamp;
        }
        // 拉完历史要把 resume 锚点对齐到最新一条，
        // 否则下次断线补发会从很旧的锚点开始，把历史又发一遍。
        // 第十六轮：这里必须**只前不后** —— 刚打开的这个会话可能是很旧的，
        // 直接赋值会把锚点往回退，下次 resume 就把已经看过的消息又补发一遍。
        _advanceAnchor(store.last.msgId);
      }
      notifyListeners();
    } catch (e) {
      lastErrorText = e.toString();
      notifyListeners();
    }
  }

  /// 本地清空一个会话的消息（用户 2026-10-04「清空聊天记录」）。
  ///
  /// 服务端那边由 `POST /api/conversations/{id}/clear` 负责（消息、摘要、长期记忆、
  /// 还没发出去的延迟消息一起清）；这里只处理**本机这份缓存**：
  /// 消息列表、去重集合、打字机队列都要丢掉，否则清空之后屏幕上还留着旧消息，
  /// 或者排队中的旧消息又一条条"打字"出来。
  ///
  /// 注意：**不动 resume 锚点**（`_lastMsgId`）。锚点只前不后是为了防重复补发；
  /// 清空是"以后不再显示"，锚点保持最新才不会让服务端把清空前的历史再补一遍。
  void clearConversation(int conversationId) {
    (_writers[conversationId])?.clear();
    _writers.remove(conversationId);
    _messages.remove(conversationId);
    _seen.remove(conversationId);
    final Conversation? c = conversationById(conversationId);
    if (c != null) {
      c.lastMessage = null;
      c.lastMessageAt = null;
      c.unreadCount = 0;
    }
    notifyListeners();
  }

  // ------------------------------------------------------------ 发送

  /// 用户自己发的消息：本地立即上屏（乐观更新），不走打字机。
  ///
  /// [contentType] 为 image 时 [text] 是 data URL（见 models/message.dart）。
  void addLocalUserMessage({
    required String clientMsgId,
    required String text,
    String contentType = 'text',
    int? conversationId,
  }) {
    final int cid = conversationId ?? conversation.id;
    final ChatMessage m = ChatMessage.localUser(
      conversationId: cid,
      clientMsgId: clientMsgId,
      text: text,
      timestamp: DateTime.now(),
      contentType: contentType,
    );
    (_seen[cid] ??= <String>{}).add('$clientMsgId#0');
    (_messages[cid] ??= <ChatMessage>[]).add(m);
    final Conversation? c = conversationById(cid);
    if (c != null) {
      c.lastMessage = m;
      c.lastMessageAt = m.timestamp;
    }
    _sortConversations();
    notifyListeners();
  }

  // ------------------------------------------------------------ 服务端消息

  /// 这个会话里对方的 users.id（不是人类好友会话时为 0）。
  ///
  /// 2026-09-30 第十四轮：真人好友会话里双方消息的 sender_type 都是 user，
  /// 未读计数与「自己的回声」判定都不能再用 sender_type 兜底，必须问这个。
  int _peerUserIdOf(int conversationId) =>
      conversationById(conversationId)?.peerUserId ?? 0;

  /// 处理 WS-001 3.3 的 message：拆段 -> 去重 -> 入打字机队列。
  ///
  /// 按 conversation_id 分发到对应会话；不在聊天页的会话累计未读（PRD 3.1.3）。
  ///
  /// [historyOnly] = 这一批是「没有锚点时的全量补发」，只对齐锚点、不上屏（第十六轮）。
  void ingestServerMessage(
    Map<String, dynamic> json, {
    bool fromResume = false,
    bool historyOnly = false,
  }) {
    final List<ChatMessage> segments = ChatMessage.fromWsJson(json);
    if (segments.isEmpty) return;

    final String msgId = segments.first.msgId;
    // 第十六轮：推进锚点只前不后（见 _advanceAnchor）。
    _advanceAnchor(msgId);
    // 实时推来的消息说明锚点已经真实存在了（这一条自己就是锚点），
    // 之后的重连补发才有资格被当成「新消息」。否则服务端一条补发都不发时
    // （比如历史全空），我们会一直以为手上没有锚点，把真正的错过消息吞掉。
    if (!fromResume) _resumeAnchorKnown = true;

    final int cid = segments.first.conversationId;
    final int peerId = _peerUserIdOf(cid);

    if (historyOnly) {
      // 刚登录 / 重装之后本地没有锚点，服务端把**全部历史**当补发发了过来。
      // 那些不是「刚收到的消息」：不进打字机、不上屏、不计未读。
      // 只把它们记进去重集合（免得随后又推一遍时重复上屏），锚点上面已经推到了最新。
      final Set<String> seenHistory = _seen[cid] ??= <String>{};
      for (final ChatMessage s in segments) {
        seenHistory.add('${s.msgId}#${s.segmentIndex}');
      }
      return;
    }

    // 补发里可能包含用户自己发过的消息；本地已乐观上屏，这里按
    // “同文本 + 时间接近”跳过，避免重复展示（WS-001 的 message 不含 client_msg_id）。
    // 注意：只有「我自己发的」才可能是回声 —— 真人好友会话里对方的消息
    // 也是 sender_type = user，按 sender_type 判会被当成自己的回声丢掉。
    if (fromResume && segments.first.isMineFor(peerId)) {
      if (_hasLocalEcho(cid, segments.first)) return;
    }

    final Set<String> seen = _seen[cid] ??= <String>{};
    final List<ChatMessage> fresh = <ChatMessage>[];
    for (final ChatMessage s in segments) {
      final String key = '${s.msgId}#${s.segmentIndex}';
      if (seen.contains(key)) continue;
      seen.add(key);
      fresh.add(s);
    }
    if (fresh.isEmpty) return;

    // 只有「我自己发的」才算回声；真人好友发来的消息必须照常计未读
    // （2026-09-30 第十四轮：原来写成 sender_type == user，于是真人好友
    //   发来的消息永远不产生未读小红点）。
    final bool isUserEcho = fresh.first.isMineFor(peerId);
    if (!isUserEcho && viewingConversationId != cid) {
      final Conversation? c = conversationById(cid);
      if (c != null) c.unreadCount += 1;
    }
    _writerOf(cid).enqueueAll(fresh);
    final Conversation? c = conversationById(cid);
    if (c != null) c.lastMessageAt = fresh.last.timestamp;
    _sortConversations();
    notifyListeners();
  }

  bool _hasLocalEcho(int conversationId, ChatMessage incoming) {
    final int peerId = _peerUserIdOf(conversationId);
    for (final ChatMessage m in _messages[conversationId] ?? const <ChatMessage>[]) {
      // 只有我自己发过的消息才可能和它撞成「同一个回声」（第十四轮：
      // 真人好友会话里对方的消息 sender_type 也是 user，不能只看类型）。
      if (!m.isMineFor(peerId)) continue;
      // 图片消息的 text 是几百 KB 的 data URL，不逐字比较，按类型判重即可
      if (incoming.isImage) {
        if (m.isImage) return true;
        continue;
      }
      if (m.text != incoming.text) continue;
      final int diff = m.timestamp.difference(incoming.timestamp).inSeconds.abs();
      if (diff <= 120) return true;
    }
    return false;
  }

  /// 处理 resume_missed：按 timestamp 排序后逐条入队（WS-001 3.6）。
  ///
  /// 2026-09-30 第十六轮：**手上没有锚点**时（刚登录 / 重装 / 换账号），
  /// 服务端按协议会把该用户所有会话的全部消息当成补发发过来。那一批是历史，
  /// 不是「刚收到的新消息」—— 只用来对齐锚点，不上屏也不计未读
  /// （未读看服务端 unread_count，历史在用户点进会话时由 loadHistory 拉）。
  void ingestResumeMissed(List<dynamic> rawList) {
    final List<Map<String, dynamic>> items = <Map<String, dynamic>>[];
    for (final dynamic raw in rawList) {
      if (raw is Map<String, dynamic>) {
        items.add(raw);
      } else if (raw is Map) {
        items.add(Map<String, dynamic>.from(raw));
      }
    }
    items.sort((Map<String, dynamic> a, Map<String, dynamic> b) {
      final String ta = a['timestamp']?.toString() ?? '';
      final String tb = b['timestamp']?.toString() ?? '';
      return ta.compareTo(tb);
    });
    // 2026-10-01 第十六轮 / 第二十轮：
    // ①手上没有锚点（刚登录、重装、换账号）时，服务端按协议会把该用户所有会话的
    //   全部消息当成补发发过来；
    // ②就算有锚点，锚点在服务端找不到时（那条消息被删了）服务端也会返回全部。
    // 两种情况下这批都是**历史**，不是「刚收到的新消息」：
    // 只用来对齐锚点，不上屏、不进打字机、不计未读。
    final bool historyOnly =
        !_resumeAnchorKnown || items.length > resumeMaxAnimated;
    if (historyOnly && items.length > resumeMaxAnimated) {
      // 留一条线索：出现这种情况说明锚点和服务端对不上（或者刚登录没锚点），
      // 排障时一眼能看出来。
      debugPrint('补发 ${items.length} 条（阈值 $resumeMaxAnimated），按历史处理：'
          '不上屏、不弹消息；锚点已对齐到最新一条');
    }
    for (final Map<String, dynamic> item in items) {
      ingestServerMessage(item, fromResume: true, historyOnly: historyOnly);
    }
    // 这一批之后手上就有真锚点了：以后的重连才是「真的错过几条」。
    _resumeAnchorKnown = true;
  }

  // ------------------------------------------------------------ 未读

  /// 一批补发超过这个条数，就当成「历史」而不是「刚错过的消息」。
  ///
  /// 2026-10-01 第二十轮（用户再次报「一进来 90 多条看过的消息」）：
  /// 锚点在服务端**找不到**时（比如那条消息后来被删了、或者服务端换过库），
  /// 按 WS-001 3.5 服务端会把「该用户所有会话的全部消息」发回来 ——
  /// 客户端就会拿 90 多条当新消息一条条弹。真正错过的消息通常只有几条，
  /// 所以超过这个数一律按历史处理：不进打字机、不上屏、不计未读
  /// （未读以服务端 unread_count 为准，历史在点进会话时由 loadHistory 拉）。
  static const int resumeMaxAnimated = 20;

  /// 进入聊天页：记下正在看的会话并把未读清零（PRD 3.1.6）。
  ///
  /// 2026-10-01 第二十轮：**同时告诉服务端**（POST /api/conversations/{id}/read）。
  /// 这里原来是只清本地的 —— 服务端的 unread_count 只涨不落，
  /// 于是每次重新登录，applyConversations 又会把那个 90 多的数字塞回本地，
  /// 表现就是用户报的「看过的消息又变成新消息，一次 90 多条」。
  void enterChat(int conversationId) {
    viewingConversationId = conversationId;
    final Conversation? c = conversationById(conversationId);
    if (c != null && c.unreadCount != 0) {
      c.unreadCount = 0;
    }
    // 不 await：进页面的动作不能被网络挡住；失败也不影响本地已经清零。
    unawaited(_markReadOnServer(conversationId));
    notifyListeners();
  }

  Future<void> _markReadOnServer(int conversationId) async {
    if (conversationId <= 0) return;
    try {
      await ApiClient.instance.markRead(conversationId);
    } catch (_) {
      // 离线时清不掉很正常：下次进这个会话还会再清一次。
    }
  }

  void leaveChat() {
    viewingConversationId = 0;
  }

  void setError(String text) {
    lastErrorText = text;
    notifyListeners();
  }

  /// 重新从服务端拉一次会话列表（改完会话设置后调用）。
  ///
  void applyConfig() {
    refreshConversations();
  }

  /// 从列表里去掉一个会话（删好友 / 删人类好友后调用）。
  ///
  /// 服务端已经删了，本地再留着只会让用户点进去看到一个空白页，
  /// 而且下次 refresh 之前它会一直挂在列表上。
  void removeConversation(int conversationId) {
    _conversations.removeWhere((Conversation c) => c.id == conversationId);
    _messages.remove(conversationId);
    _seen.remove(conversationId);
    if (viewingConversationId == conversationId) viewingConversationId = 0;
    notifyListeners();
  }

  /// 人类好友列表里的“对方在线”变了（首屏 sync 时用）。
  void updatePeerOnline(int conversationId, bool online) {
    final Conversation? c = conversationById(conversationId);
    if (c == null || c.peerOnline == online) return;
    c.peerOnline = online;
    notifyListeners();
  }
}
