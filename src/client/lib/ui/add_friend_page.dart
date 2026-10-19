// 加好友页（用户 2026-09-27 要求：像 QQ 一样进去什么都没有，自己加好友）。
//
// -（QQ 界面复刻）、3.4.1（多个 AI 进同一个群聊）
// - 人格导入（会话设置里导入聊天记录生成人格）
//
// 2026-09-27 第七轮：这一页分成**上下两块**，因为“好友”现在有两种：
//
//   一、AI 朋友
//       · 新建：填昵称 -> 建 AI 档案 -> 给它建单聊；
//       · 已有的 AI 列表里点「加为好友」-> 建单聊（服务端幂等，点两次不会重复）。
//       头像/人格/API 全部在**这个聊天的右上角设置**里改（用户明确限定那五项）。
//
//   二、人类账号
//       · 输入对方的 4 位账号 -> 直接成为好友（用户明确「不需要对方同意，
//         输入账号即双向生效」）；账号不存在时服务端回「账号不存在」。
//       对方也是装了同一个 App 的人，消息直投，不经 AI。
//
// 头像仍然只用本地资源（用户 2026-09-25 要求，不联网加载）：
// 新加的 AI 默认没有头像，显示中性人形轮廓；要一张脸就进会话设置从相册选。

import 'dart:io';

import 'package:flutter/material.dart';

import '../services/api_client.dart';
import '../services/local_queue.dart';

class AddFriendPage extends StatefulWidget {
  const AddFriendPage({super.key});

  @override
  State<AddFriendPage> createState() => _AddFriendPageState();
}

class _AddFriendPageState extends State<AddFriendPage> {
  final TextEditingController _name = TextEditingController();
  final TextEditingController _account = TextEditingController();

  List<Map<String, dynamic>> _profiles = <Map<String, dynamic>>[];
  List<Map<String, dynamic>> _humans = <Map<String, dynamic>>[];
  bool _loading = true;
  bool _busy = false;
  String? _error;

  @override
  void initState() {
    super.initState();
    _load();
  }

  @override
  void dispose() {
    _name.dispose();
    _account.dispose();
    super.dispose();
  }

  Future<void> _load() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final List<Map<String, dynamic>> ais =
          await ApiClient.instance.listAiProfiles();
      // 人类好友拉不到不算错：旧版本服务端没有这个接口时这一块留空即可，
      // 不该把整页变成错误页（AI 那一块还是能用的）。
      List<Map<String, dynamic>> humans = <Map<String, dynamic>>[];
      try {
        humans = await ApiClient.instance.listHumanFriends();
      } on ApiException {
        humans = <Map<String, dynamic>>[];
      }
      if (!mounted) return;
      setState(() {
        _profiles = ais;
        _humans = humans;
        _loading = false;
      });
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _loading = false;
        _error = e.toString();
      });
    }
  }

  void _toast(String text) {
    if (!mounted) return;
    ScaffoldMessenger.of(context)
      ..hideCurrentSnackBar()
      ..showSnackBar(SnackBar(content: Text(text)));
  }

  // ------------------------------------------------------------ AI 朋友

  /// 给某个已有 AI 建单聊（幂等）。
  Future<void> _addExisting(int aiProfileId, String name) async {
    if (_busy) return;
    setState(() => _busy = true);
    try {
      await ApiClient.instance.createSingleConversation(
        aiProfileId: aiProfileId,
      );
      await LocalQueue.instance.refreshConversations();
      if (!mounted) return;
      _toast('已把“$name”加进消息列表');
      Navigator.of(context).pop(true);
    } catch (e) {
      if (mounted) _toast('添加失败：$e');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  /// 新建一个 AI 再建单聊。
  Future<void> _createAndAdd() async {
    final String name = _name.text.trim();
    if (name.isEmpty) {
      _toast('先给这个朋友起个名字');
      return;
    }
    if (_busy) return;
    setState(() => _busy = true);
    try {
      final Map<String, dynamic> ai =
          await ApiClient.instance.createAiProfile(name: name);
      final int id = (ai['id'] as num?)?.toInt() ?? 0;
      if (id <= 0) throw ApiException('服务端没有返回新 AI 的 id');
      await ApiClient.instance.createSingleConversation(aiProfileId: id);
      await LocalQueue.instance.refreshConversations();
      if (!mounted) return;
      _toast('“$name”已加进消息列表');
      Navigator.of(context).pop(true);
    } catch (e) {
      if (mounted) _toast('添加失败：$e');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  // ------------------------------------------------------------ 人类账号

  /// 按 4 位账号加人。
  ///
  /// 服务端对「账号不存在」返回 404，detail 就是「账号不存在」，
  /// ApiException.message 会原样带出来，直接弹给用户。
  Future<void> _addHuman() async {
    final String acc = _account.text.trim();
    if (acc.isEmpty) {
      _toast('请输入对方的账号');
      return;
    }
    if (_busy) return;
    setState(() => _busy = true);
    try {
      final Map<String, dynamic> res =
          await ApiClient.instance.addHumanFriend(acc);
      final dynamic f = res['friend'];
      final String nick = (f is Map ? f['nickname']?.toString() : null) ?? acc;
      await LocalQueue.instance.refreshConversations();
      if (!mounted) return;
      _toast('已添加“$nick”为好友');
      Navigator.of(context).pop(true);
    } on ApiException catch (e) {
      if (mounted) _toast(e.message);
    } catch (e) {
      if (mounted) _toast('添加失败：$e');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  /// 删一个人类好友（只要有一个人删了，双方列表都没了 —— 服务端语义）。
  Future<void> _deleteHuman(int peerUserId, String name) async {
    final bool? ok = await showDialog<bool>(
      context: context,
      builder: (BuildContext ctx) => AlertDialog(
        title: const Text('删除这位好友？'),
        content: Text('「$name」和你的聊天记录会被删掉，双方列表里都会消失，不能恢复。'),
        actions: <Widget>[
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(false),
            child: const Text('取消'),
          ),
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(true),
            style: TextButton.styleFrom(
              foregroundColor: const Color(0xFFD32F2F),
            ),
            child: const Text('删除'),
          ),
        ],
      ),
    );
    if (ok != true || !mounted) return;
    setState(() => _busy = true);
    try {
      await ApiClient.instance.deleteHumanFriend(peerUserId);
      // 本地缓存要跟着清：服务端已经把两边的会话都删了，
      // 本地留着它会让列表上挂一条点不进去的对话。
      await LocalQueue.instance.refreshConversations();
      await _load();
      if (!mounted) return;
      _toast('已删除');
    } catch (e) {
      if (mounted) _toast('删除失败：$e');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  // ------------------------------------------------------------ UI

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: Colors.white,
      appBar: AppBar(
        backgroundColor: const Color(0xFFF7F7F7),
        elevation: 0.5,
        foregroundColor: Colors.black87,
        title: const Text('加好友', style: TextStyle(fontSize: 16)),
      ),
      body: _loading
          ? const Center(child: CircularProgressIndicator())
          : ListView(
              padding: const EdgeInsets.all(16),
              children: <Widget>[
                if (_error != null) _errorCard(_error!),
                ..._aiSection(),
                const SizedBox(height: 26),
                const Divider(),
                const SizedBox(height: 10),
                ..._humanSection(),
                const SizedBox(height: 30),
              ],
            ),
    );
  }

  // ---------------- AI 朋友 ----------------

  List<Widget> _aiSection() {
    return <Widget>[
      _label('AI 朋友'),
      const Text(
        '自己捏一个人出来聊。头像、性格、用哪个模型，都在那个聊天的右上角设置里改。',
        style: TextStyle(fontSize: 12, color: Color(0xFF999999)),
      ),
      const SizedBox(height: 10),
      Row(
        children: <Widget>[
          Expanded(
            child: TextField(
              controller: _name,
              decoration: _decoration('给他起个名字，比如：老王'),
              onSubmitted: (_) => _createAndAdd(),
            ),
          ),
          const SizedBox(width: 10),
          ElevatedButton(
            onPressed: _busy ? null : _createAndAdd,
            style: ElevatedButton.styleFrom(
              backgroundColor: const Color(0xFF0099FF),
              foregroundColor: Colors.white,
              padding:
                  const EdgeInsets.symmetric(horizontal: 18, vertical: 13),
            ),
            child: const Text('添加'),
          ),
        ],
      ),
      const SizedBox(height: 18),
      if (_profiles.isEmpty)
        const Text('还没有 AI 朋友，在上面新建一个',
            style: TextStyle(fontSize: 12, color: Color(0xFF999999))),
      for (final Map<String, dynamic> p in _profiles) _profileTile(p),
    ];
  }

  Widget _profileTile(Map<String, dynamic> p) {
    final int id = (p['id'] as num?)?.toInt() ?? 0;
    final String name = p['name']?.toString() ?? 'AI';
    final String? avatar = _nonEmpty(p['avatar_url']);
    return _card(
      avatarPath: avatar,
      title: name,
      subtitle: 'AI 朋友',
      trailing: TextButton(
        onPressed: _busy ? null : () => _addExisting(id, name),
        child: const Text('加为好友'),
      ),
    );
  }

  // ---------------- 人类账号 ----------------

  List<Widget> _humanSection() {
    return <Widget>[
      _label('人类账号'),
      const Text(
        '对方也装了这个软件，就有自己的 4 位账号。填进他的账号，直接就成好友了，'
        '不用等他同意；他的账号也同时会多出你。',
        style: TextStyle(fontSize: 12, color: Color(0xFF999999)),
      ),
      const SizedBox(height: 10),
      Row(
        children: <Widget>[
          Expanded(
            child: TextField(
              controller: _account,
              keyboardType: TextInputType.number,
              decoration: _decoration('对方的 4 位账号'),
              onSubmitted: (_) => _addHuman(),
            ),
          ),
          const SizedBox(width: 10),
          ElevatedButton(
            onPressed: _busy ? null : _addHuman,
            style: ElevatedButton.styleFrom(
              backgroundColor: const Color(0xFF0099FF),
              foregroundColor: Colors.white,
              padding:
                  const EdgeInsets.symmetric(horizontal: 18, vertical: 13),
            ),
            child: const Text('加好友'),
          ),
        ],
      ),
      const SizedBox(height: 18),
      if (_humans.isEmpty)
        const Text('还没有人类好友',
            style: TextStyle(fontSize: 12, color: Color(0xFF999999))),
      for (final Map<String, dynamic> h in _humans) _humanTile(h),
    ];
  }

  Widget _humanTile(Map<String, dynamic> h) {
    final int uid = (h['user_id'] as num?)?.toInt() ?? 0;
    final String account = h['account_id']?.toString() ?? '';
    final String nick = h['nickname']?.toString() ?? account;
    final bool online = h['online'] == true;
    return _card(
      avatarPath: _nonEmpty(h['avatar_url']),
      title: nick,
      subtitle: '账号 $account · ${online ? '在线' : '离线'}',
      subtitleColor: online ? const Color(0xFF07C160) : null,
      trailing: TextButton(
        onPressed: _busy ? null : () => _deleteHuman(uid, nick),
        style: TextButton.styleFrom(
          foregroundColor: const Color(0xFFD32F2F),
        ),
        child: const Text('删除'),
      ),
    );
  }

  // ---------------- 小零件 ----------------

  Widget _card({
    required String? avatarPath,
    required String title,
    required String subtitle,
    required Widget trailing,
    Color? subtitleColor,
  }) {
    return Container(
      margin: const EdgeInsets.only(bottom: 10),
      padding: const EdgeInsets.all(10),
      decoration: BoxDecoration(
        color: const Color(0xFFFAFAFA),
        borderRadius: BorderRadius.circular(8),
      ),
      child: Row(
        children: <Widget>[
          _avatar(avatarPath),
          const SizedBox(width: 12),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: <Widget>[
                Text(title,
                    overflow: TextOverflow.ellipsis,
                    style: const TextStyle(
                        fontSize: 15, fontWeight: FontWeight.w500)),
                const SizedBox(height: 2),
                Text(subtitle,
                    style: TextStyle(
                        fontSize: 12,
                        color: subtitleColor ?? const Color(0xFF999999))),
              ],
            ),
          ),
          trailing,
        ],
      ),
    );
  }

  /// 头像：有本地文件就用文件，没有就中性人形轮廓（不拿内置图顶替）。
  Widget _avatar(String? path) {
    final bool exists = path != null && File(path).existsSync();
    return CircleAvatar(
      radius: 20,
      backgroundColor: const Color(0xFFE8EEF9),
      child: ClipOval(
        child: exists
            ? Image.file(
                File(path),
                width: 40,
                height: 40,
                fit: BoxFit.cover,
                errorBuilder: (_, _, _) => const Icon(Icons.person,
                    size: 20, color: Color(0xFFB9C2CF)),
              )
            : const Icon(Icons.person, size: 20, color: Color(0xFFB9C2CF)),
      ),
    );
  }

  static String? _nonEmpty(dynamic v) {
    final String s = v?.toString().trim() ?? '';
    return s.isEmpty ? null : s;
  }

  Widget _label(String text) => Padding(
        padding: const EdgeInsets.only(bottom: 6),
        child: Text(text,
            style: const TextStyle(
                fontSize: 15,
                fontWeight: FontWeight.w600,
                color: Color(0xFF222222))),
      );

  InputDecoration _decoration(String hint) => InputDecoration(
        hintText: hint,
        hintStyle: const TextStyle(fontSize: 13, color: Color(0xFFBBBBBB)),
        isDense: true,
        contentPadding:
            const EdgeInsets.symmetric(horizontal: 12, vertical: 12),
        border: OutlineInputBorder(
          borderRadius: BorderRadius.circular(6),
          borderSide: const BorderSide(color: Color(0xFFDDDDDD)),
        ),
      );

  Widget _errorCard(String text) => Container(
        width: double.infinity,
        margin: const EdgeInsets.only(bottom: 16),
        padding: const EdgeInsets.all(12),
        decoration: BoxDecoration(
          color: const Color(0xFFFFECEC),
          borderRadius: BorderRadius.circular(8),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: <Widget>[
            const Text('读不到朋友列表',
                style: TextStyle(
                    fontSize: 13,
                    fontWeight: FontWeight.w600,
                    color: Color(0xFF9B1C1C))),
            const SizedBox(height: 4),
            Text(text,
                style: const TextStyle(
                    fontSize: 12, color: Color(0xFF9B1C1C))),
            const SizedBox(height: 8),
            OutlinedButton(onPressed: _load, child: const Text('重试')),
          ],
        ),
      );
}
