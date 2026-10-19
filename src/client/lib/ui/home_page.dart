// 消息列表页：会话列表、未读标记与群聊入口。
//
// 依据：（顶部导航栏、会话列表）、3.1.3（未读红点）、
//      3.1.6（按最后一条消息时间排序、未读管理）、3.4.1（群聊创建）。
//
// 2026-09-25 第二次 PRD 验证改动：
// 早期消息列表固定只有一条会话，群聊功能要求
// “用户可在设置中创建群聊、多个 AI 进入同一个群聊”，列表必须能放多个会话。
//
// 2026-09-27 改动（用户指着 QQ 截图提的要求：“UI沿用，方式也沿用”）：
// 1. 顶栏改成 QQ 那样：左边一个头像 + 昵称，昵称下面一行是「绿点 + 在线」，
//    取代原来右上角那个「已连接」小字。
// 2. 右上角只留一个「＋」（对齐 QQ），点开是 QQ 式扩展菜单：
//    加好友 / 发起群聊 / 设置。
// 3. 首次进入不再自带任何会话 —— 像 QQ 新号一样是空的，要自己加好友、自己建群。
//
// 2026-09-27 再改（用户指出两个真问题）：
// 1. 顶栏原来显示的是 AI 的昵称（小真）和 AI 的头像，等于把用户自己的身份
//    强行变成了“小真”。现在改成显示**用户自己**的昵称和头像
//    （AppConfig.userName / userAvatarImage，首次安装时由 ProfilePage 采集）。
// 2. 点顶栏这一行不再是弹连接详情，而是进「个人资料」页改昵称和头像

import 'dart:io';

import 'package:flutter/material.dart';

import 'package:intl/intl.dart';

import '../config/app_config.dart';
import '../models/conversation.dart';
import '../services/avatar_cache.dart';
import '../services/local_queue.dart';
import '../services/ws_client.dart';
import 'add_friend_page.dart';
import 'chat_page.dart';
import 'group_create_page.dart';
import 'profile_page.dart';
import 'settings_page.dart';
import 'widgets/red_dot.dart';

class HomePage extends StatefulWidget {
  const HomePage({super.key});

  @override
  State<HomePage> createState() => _HomePageState();
}

class _HomePageState extends State<HomePage> {
  final LocalQueue _queue = LocalQueue.instance;
  final WsClient _ws = WsClient.instance;

  @override
  void initState() {
    super.initState();
    _queue.addListener(_onChanged);
    _ws.addListener(_onChanged);
    // 2026-09-28 第八轮：必须也监听 AppConfig。
    //
    // 用户实测「改了昵称/头像没反应」的直接原因就在这里 —— 顶栏读的是
    // AppConfig.userName / userAvatarImage，但本页只订阅了 _queue 和 _ws。
    // 在个人资料页改完昵称，AppConfig 确实 notifyListeners 了，
    // 本页没订阅，于是不重建，看上去「没生效」；退出重进才变。
    AppConfig.instance.addListener(_onChanged);
    // 会话列表来自服务端；失败时保持空列表（不伪造会话），界面不白屏。
    _queue.refreshConversations();
  }

  @override
  void dispose() {
    _queue.removeListener(_onChanged);
    _ws.removeListener(_onChanged);
    AppConfig.instance.removeListener(_onChanged);
    super.dispose();
  }

  void _onChanged() {
    if (mounted) setState(() {});
  }

  String _timeText(Conversation c) {
    final DateTime? t = c.lastMessageAt;
    if (t == null) return '';
    final DateTime now = DateTime.now();
    if (t.year == now.year && t.month == now.month && t.day == now.day) {
      return DateFormat('HH:mm').format(t);
    }
    return DateFormat('MM-dd').format(t);
  }

  // ------------------------------------------------------------ 顶栏状态

  /// 顶栏那一行「绿点 + 在线」的状态文字（QQ 用“在线”，我们沿用）。
  String get _presenceText {
    if (_ws.isConnected) return '在线';
    if (_ws.isConnecting) return '连接中';
    return '离线';
  }

  Color get _presenceColor {
    if (_ws.isConnected) return const Color(0xFF07C160); // QQ 绿
    if (_ws.isConnecting) return const Color(0xFFB8860B);
    return const Color(0xFFBBBBBB);
  }

  /// 点顶栏的头像/昵称：进个人资料页改自己的昵称和头像（用户 2026-09-27 要求）。
  Future<void> _openProfile() async {
    await Navigator.of(context).push(
      MaterialPageRoute<bool>(
        builder: (_) => const ProfilePage(isFirstRun: false),
      ),
    );
    if (mounted) setState(() {});
  }

  // ------------------------------------------------------------ 导航

  Future<void> _openSettings() async {
    await Navigator.of(context).push(
      MaterialPageRoute<void>(builder: (_) => const SettingsPage()),
    );
    if (!mounted) return;
    await _queue.refreshConversations();
    setState(() {});
  }

  Future<void> _openCreateGroup() async {
    final bool? created = await Navigator.of(context).push<bool>(
      MaterialPageRoute<bool>(builder: (_) => const GroupCreatePage()),
    );
    if (created == true) _queue.refreshConversations();
  }

  Future<void> _openAddFriend() async {
    final bool? added = await Navigator.of(context).push<bool>(
      MaterialPageRoute<bool>(builder: (_) => const AddFriendPage()),
    );
    if (added == true) _queue.refreshConversations();
  }

  void _openChat(Conversation c) {
    Navigator.of(context).push(
      MaterialPageRoute<void>(builder: (_) => ChatPage(conversationId: c.id)),
    );
  }

  /// QQ 式「＋」菜单：一个弹出列表，不是一排图标挤在顶栏上。
  Future<void> _showPlusMenu() async {
    final RenderBox? box = context.findRenderObject() as RenderBox?;
    final RenderBox? overlay =
        Overlay.of(context).context.findRenderObject() as RenderBox?;
    if (box == null || overlay == null) return;

    final RelativeRect position = RelativeRect.fromRect(
      Rect.fromPoints(
        box.localToGlobal(box.size.topRight(Offset.zero), ancestor: overlay),
        box.localToGlobal(box.size.bottomRight(Offset.zero), ancestor: overlay),
      ),
      Offset.zero & overlay.size,
    );

    final String? picked = await showMenu<String>(
      context: context,
      position: position,
      color: Colors.white,
      items: const <PopupMenuEntry<String>>[
        PopupMenuItem<String>(
          value: 'friend',
          child: ListTile(
            dense: true,
            contentPadding: EdgeInsets.zero,
            leading: Icon(Icons.person_add_alt, size: 20),
            title: Text('加好友', style: TextStyle(fontSize: 14)),
          ),
        ),
        PopupMenuItem<String>(
          value: 'group',
          child: ListTile(
            dense: true,
            contentPadding: EdgeInsets.zero,
            leading: Icon(Icons.group_add_outlined, size: 20),
            title: Text('发起群聊', style: TextStyle(fontSize: 14)),
          ),
        ),
        PopupMenuItem<String>(
          value: 'settings',
          child: ListTile(
            dense: true,
            contentPadding: EdgeInsets.zero,
            leading: Icon(Icons.settings_outlined, size: 20),
            title: Text('设置', style: TextStyle(fontSize: 14)),
          ),
        ),
      ],
    );

    if (!mounted) return;
    switch (picked) {
      case 'friend':
        await _openAddFriend();
        break;
      case 'group':
        await _openCreateGroup();
        break;
      case 'settings':
        await _openSettings();
        break;
      default:
        break;
    }
  }

  // ------------------------------------------------------------ UI

  @override
  Widget build(BuildContext context) {
    final List<Conversation> list = _queue.conversations;

    return Scaffold(
      backgroundColor: Colors.white,
      appBar: AppBar(
        backgroundColor: const Color(0xFFF7F7F7),
        elevation: 0.5,
        foregroundColor: Colors.black87,
        toolbarHeight: 62,
        titleSpacing: 12,
        title: _title(),
        actions: <Widget>[
          IconButton(
            tooltip: '更多',
            icon: const Icon(Icons.add, size: 26),
            onPressed: _showPlusMenu,
          ),
          const SizedBox(width: 4),
        ],
      ),
      body: RefreshIndicator(
        onRefresh: _queue.refreshConversations,
        child: list.isEmpty ? _emptyView() : _listView(list),
      ),
    );
  }

  /// 顶栏：用户自己的头像 + 昵称，下面一行「绿点 + 在线」（对齐 QQ 截图）。
  ///
  /// 这里显示的必须是**用户自己**的身份（用户 2026-09-27 指出：
  /// 原来显示 AI 昵称“小真”，等于把用户强行变成了小真）。
  /// 点整行进个人资料页改昵称/头像。
  Widget _title() {
    return InkWell(
      onTap: _openProfile,
      child: Padding(
        padding: const EdgeInsets.symmetric(vertical: 6),
        child: Row(
          children: <Widget>[
            _myAvatar(radius: 18),
            const SizedBox(width: 10),
            Flexible(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.start,
                children: <Widget>[
                  Text(
                    _userDisplayName,
                    overflow: TextOverflow.ellipsis,
                    style: const TextStyle(
                        fontSize: 17, fontWeight: FontWeight.w600),
                  ),
                  const SizedBox(height: 2),
                  Row(
                    mainAxisSize: MainAxisSize.min,
                    children: <Widget>[
                      Container(
                        width: 8,
                        height: 8,
                        decoration: BoxDecoration(
                          color: _presenceColor,
                          shape: BoxShape.circle,
                        ),
                      ),
                      const SizedBox(width: 5),
                      Text(
                        _presenceText,
                        style: const TextStyle(
                            fontSize: 11, color: Color(0xFF888888)),
                      ),
                    ],
                  ),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }

  /// 空状态：什么都不显示（对齐 QQ 新号进去什么都没有）。
  ///
  /// 用户 2026-09-27 用红笔把原来那一整块都圈掉了，要求“全部不要显示”：
  /// 图标、「还没有聊天」、「点右上角「＋」加好友或发起群聊」、
  /// 「加好友 / 发起群聊」两个按钮 —— 一个都不留。
  /// 这里保留一个可滚动的空白列表，只为让下拉刷新还能用。
  Widget _emptyView() {
    return ListView(
      physics: const AlwaysScrollableScrollPhysics(),
      children: const <Widget>[SizedBox(height: 1)],
    );
  }

  Widget _listView(List<Conversation> list) {
    return ListView.separated(
      itemCount: list.length,
      separatorBuilder: (_, _) =>
          const Divider(height: 1, indent: 72, color: Color(0xFFEEEEEE)),
      itemBuilder: (BuildContext context, int index) {
        final Conversation c = list[index];
        return InkWell(
          onTap: () => _openChat(c),
          child: Container(
            color: Colors.white,
            padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
            child: Row(
              children: <Widget>[
                _avatar(c),
                const SizedBox(width: 12),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: <Widget>[
                      Row(
                        children: <Widget>[
                          Flexible(
                            child: Text(
                              // 2026-09-30 第十五轮：用 peerDisplayName 而不是 title。
                              // title 是建会话时抄下来的 AI 名字，改完名不会变；
                              // peerDisplayName 单聊直接取服务端的 ai_name。
                              c.peerDisplayName,
                              overflow: TextOverflow.ellipsis,
                              style: const TextStyle(
                                  fontSize: 16, fontWeight: FontWeight.w500),
                            ),
                          ),
                          if (c.isGroup) ...<Widget>[
                            const SizedBox(width: 6),
                            _groupTag(c),
                          ],
                          // 亲密度百分值（PRD 3.2.2）：单聊会话才显示。
                          if (c.hasSettings && c.intimacyPercent > 0) ...<Widget>[
                            const SizedBox(width: 6),
                            _intimacyTag(c),
                          ],
                        ],
                      ),
                      const SizedBox(height: 4),
                      Text(
                        c.subtitle,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(
                            fontSize: 13, color: Color(0xFF999999)),
                      ),
                    ],
                  ),
                ),
                const SizedBox(width: 10),
                Column(
                  crossAxisAlignment: CrossAxisAlignment.end,
                  children: <Widget>[
                    Text(
                      _timeText(c),
                      style: const TextStyle(
                          fontSize: 11, color: Color(0xFF999999)),
                    ),
                    const SizedBox(height: 6),
                    RedDot(count: c.unreadCount),
                  ],
                ),
              ],
            ),
          ),
        );
      },
    );
  }

  /// 群聊角标：成员数 + 是否带背景资料（PRD 3.4.1）。
  Widget _groupTag(Conversation c) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 5, vertical: 1),
      decoration: BoxDecoration(
        color: const Color(0xFFEAF3FF),
        borderRadius: BorderRadius.circular(4),
      ),
      child: Text(
        '群 ${c.participants.length}${c.hasBackground ? ' · 有背景' : ''}',
        style: const TextStyle(fontSize: 10, color: Color(0xFF3A7BD5)),
      ),
    );
  }

  Widget _intimacyTag(Conversation c) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 5, vertical: 1),
      decoration: BoxDecoration(
        color: const Color(0xFFFFEEF2),
        borderRadius: BorderRadius.circular(4),
      ),
      child: Text(
        '❤ ${c.intimacyPercent}%',
        style: const TextStyle(fontSize: 10, color: Color(0xFFD6467A)),
      ),
    );
  }

  /// 会话头像：**读这个会话自己的头像**（c.avatarUrl）。
  ///
  /// 2026-09-27 第七轮修正：以前这里调的是 _singleAvatarImage()，用的是全局
  /// AI 头像配置（AppConfig.avatarImage）。结果是所有会话共用一个头像 ——
  /// 用户在某个聊天的设置里换了“对方头像”，列表和气泡都不动。
  /// 现在头像跟着会话走（服务端把 ai_profiles.avatar_url / users.avatar_url
  /// 一路带出来，见 main._conversation_to_json）。
  ///
  /// 没有头像时**不拿内置图顶替**，只画中性人形轮廓（和“用户头像”同一条原则：
  /// 系统不替用户选一张脸）。
  Widget _avatar(Conversation c) {
    // 2026-09-28 第八轮：这里拿到的 avatarUrl 是**服务端路径**
    // （`/uploads/avatars/0001.jpg`），手机上直接 File(path) 永远不存在。
    // 必须先经 AvatarCache 下载成本地文件再 Image.file —— 否则永远画中性人形，
    // 表现就是「给好友换了头像，列表和气泡都不动」。
    final String? local = AvatarCache.instance.localPath(c.avatarUrl);
    final Widget inner;
    if (c.isGroup) {
      inner = const Icon(Icons.groups, size: 24, color: Color(0xFF5B8FF9));
    } else if (local != null) {
      inner = Image.file(
        File(local),
        width: 48,
        height: 48,
        fit: BoxFit.cover,
        errorBuilder: (_, _, _) =>
            const Icon(Icons.person, size: 24, color: Color(0xFFB9C2CF)),
      );
    } else {
      inner = const Icon(Icons.person, size: 24, color: Color(0xFFB9C2CF));
    }

    return Stack(
      children: <Widget>[
        CircleAvatar(
          radius: 24,
          backgroundColor: const Color(0xFFE8EEF9),
          child: ClipOval(child: inner),
        ),
        if (c.isGroup)
          Positioned(
            right: 0,
            bottom: 0,
            child: Container(
              padding: const EdgeInsets.all(2),
              decoration: const BoxDecoration(
                color: Colors.white,
                shape: BoxShape.circle,
              ),
              child: const Icon(Icons.people_alt,
                  size: 12, color: Color(0xFF3A7BD5)),
            ),
          ),
        if (c.isHuman && c.peerOnline)
          Positioned(
            right: 0,
            bottom: 0,
            child: Container(
              width: 12,
              height: 12,
              decoration: BoxDecoration(
                color: const Color(0xFF07C160),
                shape: BoxShape.circle,
                border: Border.all(color: Colors.white, width: 2),
              ),
            ),
          ),
      ],
    );
  }

  /// 顶栏那个「我」的显示名。
  ///
  /// 正常情况下用户第一次安装就填过（ProfilePage 不给绕过），
  /// 这里只是兜底：万一配置被清空，也不显示空白，而是提示去设置。
  String get _userDisplayName {
    final String n = AppConfig.instance.userName.trim();
    return n.isEmpty ? '点这里设置昵称' : n;
  }

  /// 顶栏自己的头像：用**用户自己**的头像（必须是他从相册选的那张）。
  ///
  /// 2026-09-27 修正：原来这里调的是 _singleAvatarImage()，用的是 AI 头像配置，
  /// 导致用户头像和 AI 头像联动、用户没法有自己的一张脸。
  ///
  /// 又一处修正（用户指出“你把用户强制性变成了那个很丑的头像”）：
  /// 没设置过头像时**不再拿内置头像顶替**，只画一个中性的人形轮廓；
  /// 用户头像必须是用户自己选的，系统不能替他选一张脸。
  Widget _myAvatar({required double radius}) {
    final ImageProvider? img = AppConfig.instance.userAvatarImage;
    return CircleAvatar(
      radius: radius,
      backgroundColor: const Color(0xFFE8EEF9),
      child: ClipOval(
        child: img == null
            ? Icon(Icons.person,
                size: radius * 1.25, color: const Color(0xFFB9C2CF))
            : Image(
                image: img,
                width: radius * 2,
                height: radius * 2,
                fit: BoxFit.cover,
                errorBuilder: (_, _, _) => Icon(Icons.person,
                    size: radius * 1.25, color: const Color(0xFFB9C2CF)),
              ),
      ),
    );
  }

  }