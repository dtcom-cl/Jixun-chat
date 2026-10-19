// 会话设置页（用户 2026-09-27 要求）。
//

import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:flutter/material.dart';

import '../services/api_client.dart';
import '../services/avatar_cache.dart';
import '../services/avatar_store.dart';
import '../services/document_picker_service.dart';
import '../services/image_picker_service.dart';
import '../services/local_queue.dart';

class ConversationSettingsPage extends StatefulWidget {
  const ConversationSettingsPage({
    super.key,
    required this.aiProfileId,
    this.conversationId,
  });

  /// 这个会话里的 AI 档案 id（单聊就一个，群聊暂时取第一个）。
  final int aiProfileId;

  /// 保存成功后通知哪个会话刷新（可以为 null）。
  final int? conversationId;

  @override
  State<ConversationSettingsPage> createState() =>
      _ConversationSettingsPageState();
}

class _ConversationSettingsPageState extends State<ConversationSettingsPage> {
  final TextEditingController _name = TextEditingController();
  final TextEditingController _baseUrl = TextEditingController();
  final TextEditingController _apiKey = TextEditingController();
  final TextEditingController _model = TextEditingController();
  final TextEditingController _personality = TextEditingController();

  /// 聊天记录原文（用来生成人格）。
  final TextEditingController _chatLog = TextEditingController();

  /// PRD 5.2：人格导入必须先确认已获得对方授权，服务端对 false 一律 403。
  bool _authorized = false;

  /// 正在调蒸馏接口（按钮转圈并禁用）。
  bool _distilling = false;

  /// 服务端认识的厂家列表。
  ///
  /// 只放 deepseek / openai / glm：这三家服务端有内置地址，选完不用手填 Base URL。
  /// 另外给一个「自动识别」——用户粘的 Key 形如 sk-xxx / glm-xxx 时，
  /// 服务端能按格式猜出厂家，猜不出才要求他手填地址。
  /// 自定义中转站走「其他」+ 手动填地址。
  static const List<String> _providers = <String>[
    'auto',
    'deepseek',
    'openai',
    'glm',
    'custom',
  ];

  static const Map<String, String> _providerLabels = <String, String>{
    'auto': '自动识别（按 Key 格式）',
    'deepseek': 'DeepSeek',
    'openai': 'OpenAI',
    'glm': '智谱 GLM',
    'custom': '其他 / 自建中转',
  };

  String _provider = 'auto';

  /// 本地新选的头像文件路径；没换头像时为 null。
  String? _newAvatarFile;

  bool _loading = true;
  bool _saving = false;

  /// 正在调删除接口（按钮转圈并禁用，防止连点两次）。
  bool _deleting = false;

  String? _error;

  /// 服务端回的 Key 尾部四位，用来提示“已经有一个 Key 了”。
  bool _hasKey = false;
  String _keyTail = '';

  /// 服务端按 Key 格式识别出的厂家，用来给用户一句提示。
  String _detected = '';

  @override
  void initState() {
    super.initState();
    _load();
  }

  @override
  void dispose() {
    _name.dispose();
    _baseUrl.dispose();
    _apiKey.dispose();
    _model.dispose();
    _personality.dispose();
    _chatLog.dispose();
    super.dispose();
  }

  /// 从聊天记录文本生成人格（PRD 3.3.4）。
  ///
  /// 这里是「原料 -> 成品」：服务端拿到文本后调模型蒸馏出语气、称呼、
  /// 回复率、首响延迟与段间隔，再落进人格档案。想直接改成品请用上面的编辑框。
  Future<void> _distillFromChatLog() async {
    final String log = _chatLog.text.trim();
    if (log.isEmpty) {
      _toast('先把聊天记录粘进来（或在下面从文件导入）');
      return;
    }
    if (!_authorized) {
      // PRD 5.2 是硬要求，服务端也会拦一次；这里先拦是为了少跑一趟网络。
      _toast('请先勾选「我确认已获得对方授权」');
      return;
    }
    setState(() => _distilling = true);
    try {
      final Map<String, dynamic> res = await ApiClient.instance.importPersonality(
        aiProfileId: widget.aiProfileId,
        friendName: _name.text.trim(),
        chatLog: log,
        authorized: true,
      );
      if (!mounted) return;
      // 服务端返回的是新人格；直接回填编辑框，用户能立刻看到学到了什么。
      final Object? pd = res['personality_data'];
      setState(() {
        _personality.text = pd == null
            ? _personality.text
            : const JsonEncoder.withIndent('  ').convert(pd);
      });
      _toast('人格已生成（用了 ${res['records'] ?? '?'} 条记录）');
    } on ApiException catch (e) {
      if (!mounted) return;
      // 2026-09-29 第十二轮：超时要单独说话。
      //
      // 服务端是**同步**跑蒸馏的，客户端超时挂断**不会**让它停下 ——
      // 它会继续跑完并把新人格存进库（电脑端日志里照旧「蒸馏成功」）。
      // 这时候编辑框里还是**旧的那份 JSON**，用户如果顺手点「保存」，
      // 就会把刚蒸馏好的新人格又覆盖回旧的。所以这里明确告诉他：
      // 别急着保存，先退出重进看一眼。
      if (e.message.contains('超时')) {
        _toast('等待超时了。服务端可能还在生成，请退出这个页面重进，看人格是否已更新；'
            '确认之前先别点「保存」，否则会把刚生成的人格覆盖掉');
      } else {
        _toast(e.message);
      }
    } catch (e) {
      if (mounted) _toast('生成失败：$e');
    } finally {
      if (mounted) setState(() => _distilling = false);
    }
  }

  /// 从手机里选一个文本文件读进来（聊天记录导出成 txt 的情况）。
  Future<void> _importChatLogFile() async {
    final DocumentPickResult r = await DocumentPickerService.pickTextFile();
    if (!mounted) return;
    if (r.unsupported) {
      _toast('当前平台不支持选文件，请在 Android 真机上使用');
      return;
    }
    if (r.cancelled) return;
    if (!r.isOk) {
      _toast('读取失败：${r.error}');
      return;
    }
    setState(() => _chatLog.text = r.text!);
    _toast('已读入 ${r.fileName ?? '文件'}，点「导入并生成人格」');
  }

  void _toast(String text) {
    if (!mounted) return;
    ScaffoldMessenger.of(context)
      ..hideCurrentSnackBar()
      ..showSnackBar(SnackBar(content: Text(text)));
  }

  Future<void> _load() async {
    try {
      final Map<String, dynamic> s =
          await ApiClient.instance.getAiSettings(widget.aiProfileId);
      if (!mounted) return;
      setState(() {
        _name.text = (s['name'] ?? '').toString();
        final String p = (s['api_provider'] ?? '').toString();
        _provider = _providers.contains(p) ? p : 'auto';
        _baseUrl.text = (s['api_base_url'] ?? '').toString();
        _model.text = (s['api_model'] ?? '').toString();
        _hasKey = s['has_key'] == true;
        _keyTail = (s['key_tail'] ?? '').toString();
        _detected = (s['detected_provider'] ?? '').toString();
        // 服务端存的是本地文件路径（不联网加载）；空串按“没有”处理。
        final String savedAvatar = (s['avatar_url'] ?? '').toString().trim();
        _serverAvatarPath = savedAvatar.isEmpty ? null : savedAvatar;
        // 人格数据可能很大，格式化后展示更好读；不是合法 JSON 就原样显示。
        final Object? pd = s['personality_data'];
        _personality.text = pd == null
            ? ''
            : (pd is String
                ? pd
                : const JsonEncoder.withIndent('  ').convert(pd));
        _loading = false;
      });
    } on ApiException catch (e) {
      if (!mounted) return;
      // 2026-09-28 第八轮：404 要给一句人话并自动退出去。
      //
      // 为什么需要：用户实测「点进去显示 AI 档案不存在」——
      // 那个 AI 根本不属于当前账号（本地会话缓存没清干净时点进来的）。
      // 原来这里把 `ApiException(404: ...)` 整串 toString 摊在屏幕上，
      // 用户看到的是堆栈式英文，不知道该干什么。
      // 现在直接说明白 + 自动返回，不让他停在一个永远填不了的页面上。
      if (e.statusCode == 404) {
        setState(() {
          _loading = false;
          _error = '这个好友已经不存在了（可能被删了，或属于另一个账号）。';
        });
        _toast('这个好友已经不存在了');
        await Future<void>.delayed(const Duration(milliseconds: 900));
        if (!mounted) return;
        Navigator.of(context).pop('deleted');
        return;
      }
      setState(() {
        _loading = false;
        _error = e.message;
      });
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _loading = false;
        _error = e.toString();
      });
    }
  }

  Future<void> _pickAvatar() async {
    final PickResult r = await ImagePickerService.pickFromGallery();
    if (!mounted) return;
    if (r.unsupported) {
      _toast('当前平台不支持选相册，请在 Android 真机上使用');
      return;
    }
    if (r.cancelled) return;
    if (!r.isOk) {
      _toast('选图失败：${r.error}');
      return;
    }
    final String? path = await AvatarStore.save(r.image!.bytes);
    if (!mounted) return;
    if (path == null) {
      _toast('头像保存失败，换一张试试');
      return;
    }
    setState(() => _newAvatarFile = path);
    _toast('新头像已选好，点“保存”生效');
  }

  /// 把刚选的头像读成 data URL（`data:image/jpeg;base64,...`）。
  ///
  /// 2026-09-28 第九轮新增。以前是把这个文件的**手机本地路径**当 avatar_url
  /// 传给服务端，服务端存下来既打不开、客户端又拿它去拼服务器地址请求，
  /// 结果日志里全是 404。改成传字节，服务端落盘后返回自己的 `/uploads/` 路径。
  ///
  /// 没有新选头像（`_newAvatarFile == null`）时返回 null，表示「这一项不改」。
  Future<String?> _readAvatarAsDataUrl() async {
    final String? path = _newAvatarFile;
    if (path == null || path.isEmpty) return null;
    try {
      final File f = File(path);
      if (!f.existsSync()) return null;
      final Uint8List bytes = await f.readAsBytes();
      if (bytes.isEmpty) return null;
      return 'data:image/${_avatarExt()};base64,${base64Encode(bytes)}';
    } catch (_) {
      return null;
    }
  }

  /// 按文件后缀猜图片格式（服务端只认 jpg/png/webp/gif，其余回落 jpg）。
  String _avatarExt() {
    final String p = (_newAvatarFile ?? '').toLowerCase();
    for (final String ext in <String>['png', 'webp', 'gif', 'jpg', 'jpeg']) {
      if (p.endsWith('.$ext')) return ext == 'jpeg' ? 'jpg' : ext;
    }
    return 'jpg';
  }

  Future<void> _save() async {
    final String name = _name.text.trim();
    if (name.isEmpty) {
      _toast('对方的名字不能为空');
      return;
    }

    // 人格数据必须是合法 JSON 对象，否则服务端 normalize 会把它重置成默认人格，
    // 用户会以为“改坏了”。这里先拦一道，把话说清楚。
    final String pd = _personality.text.trim();
    if (pd.isNotEmpty) {
      try {
        final Object? decoded = jsonDecode(pd);
        if (decoded is! Map) {
          _toast('人格数据要是一个 JSON 对象（以 { 开头、以 } 结尾）');
          return;
        }
      } catch (_) {
        _toast('人格数据不是合法 JSON，检查一下括号和引号');
        return;
      }
    }

    setState(() => _saving = true);
    try {
      // 头像走**字节上传**，不传手机本地路径。
      //
      // 2026-09-28 第九轮修的 bug：原来这里传的是 `_newAvatarFile`，
      // 也就是 `/data/user/0/com.jixun.jixun_client/code_cache/.../ai_avatar.jpg`
      // 这种**手机私有目录的路径**。服务端照存不误，于是：
      //   1. 服务端上根本没有这张图（存的是别人手机里的路径）；
      //   2. 客户端 AvatarCache 又给这个路径拼上服务器地址去请求，
      //      服务端日志里就刷 `GET /data/user/0/... 404 Not Found`。
      // 现在把文件读成字节、按 data URL 传上去，服务端落盘后回一个
      // `/uploads/avatars/ai<id>.jpg`，两边都能用。
      final String? avatarData = await _readAvatarAsDataUrl();
      final Map<String, dynamic> saved =
          await ApiClient.instance.updateAiSettings(
        widget.aiProfileId,
        name: name,
        avatarUrl: null,
        avatarBase64: avatarData,
        avatarExt: avatarData == null ? null : _avatarExt(),
        apiProvider: _provider,
        apiBaseUrl: _baseUrl.text.trim(),
        apiModel: _model.text.trim(),
        // 输入框为空 = 不动原来的 Key；想清空请用下面的“清除 Key”。
        apiKey: _apiKey.text.trim().isEmpty ? null : _apiKey.text.trim(),
      );

      // 2026-09-30 第十五轮（用户报告「给模型更换头像之后聊天界面不会实时更新」）：
      // 服务端存头像是**覆盖同一个文件名**（`/uploads/avatars/ai7.jpg`），换完
      // URL 一个字都没变，而客户端头像是按 URL 缓存成本地文件的 —— 不主动失效的话，
      // 聊天气泡和会话列表上永远是上一张脸（重装 App 才会变）。
      // 老 URL 和新 URL 都失效一次：换了后缀（jpg->png）时 URL 会变。
      final String newAvatar = (saved['avatar_url'] ?? '').toString().trim();
      if (avatarData != null) {
        await AvatarCache.instance.invalidate(_serverAvatarPath);
        await AvatarCache.instance.invalidate(newAvatar);
      }
      _serverAvatarPath = newAvatar.isEmpty ? null : newAvatar;
      if (pd.isNotEmpty) {
        await ApiClient.instance.importPersonalityRaw(
          aiProfileId: widget.aiProfileId,
          personalityData: pd,
        );
      }
      if (!mounted) return;
      // 会话标题跟着名字走（PRD 3.1.2：改 AI 昵称，会话列表要同步）。
      LocalQueue.instance.applyConfig();
      final int? cid = widget.conversationId;
      if (cid != null) await LocalQueue.instance.refreshConversations();
      if (!mounted) return;
      _apiKey.clear();
      _toast('已保存');
      // 回 'saved' 而不是 true：聊天页要能区分「保存」和「删除」。
      Navigator.of(context).pop('saved');
    } catch (e) {
      if (mounted) _toast('保存失败：$e');
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  Future<void> _clearKey() async {
    final bool? ok = await showDialog<bool>(
      context: context,
      builder: (BuildContext ctx) => AlertDialog(
        title: const Text('清除 API Key？'),
        content: const Text('清掉之后，这个聊天如果没有全局 API 配置就不会再回复。'),
        actions: <Widget>[
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(false),
            child: const Text('取消'),
          ),
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(true),
            child: const Text('清除'),
          ),
        ],
      ),
    );
    if (ok != true) return;
    try {
      await ApiClient.instance
          .updateAiSettings(widget.aiProfileId, apiKey: '');
      if (!mounted) return;
      setState(() {
        _hasKey = false;
        _keyTail = '';
        _detected = '';
      });
      _toast('已清除 API Key');
    } catch (e) {
      if (mounted) _toast('清除失败：$e');
    }
  }

  /// 头像预览：优先刚选的新头像（本地文件），其次服务端那张（走 AvatarCache
  /// 下载成本地文件），最后给个中性占位。
  ///
  /// 2026-09-28 第九轮：服务端存的是 `/uploads/avatars/ai7.jpg` 这种**服务端路径**，
  /// 手机上 `File(path)` 永远不存在 —— 原来这里就直接画人形轮廓，用户以为没存上。
  /// 现在过一遍 AvatarCache。
  Widget _avatarPreview() {
    final String? localPath = _newAvatarFile;
    final bool hasLocal =
        localPath != null && localPath.isNotEmpty && File(localPath).existsSync();
    final String? cached =
        hasLocal ? null : AvatarCache.instance.localPath(_serverAvatarPath);
    final String? path = hasLocal ? localPath : cached;
    final bool exists = path != null && path.isNotEmpty && File(path).existsSync();
    return CircleAvatar(
      radius: 34,
      backgroundColor: const Color(0xFFE8EEF9),
      child: ClipOval(
        child: exists
            ? Image.file(
                File(path),
                width: 68,
                height: 68,
                fit: BoxFit.cover,
                errorBuilder: (_, _, _) => const Icon(Icons.person,
                    size: 34, color: Color(0xFFB9C2CF)),
              )
            : const Icon(Icons.person, size: 34, color: Color(0xFFB9C2CF)),
      ),
    );
  }

  /// 服务端存的头像路径（`/uploads/...`，需要经 AvatarCache 下载）。
  String? _serverAvatarPath;

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: Colors.white,
      appBar: AppBar(
        backgroundColor: const Color(0xFFF7F7F7),
        elevation: 0.5,
        foregroundColor: Colors.black87,
        title: const Text('聊天设置', style: TextStyle(fontSize: 16)),
      ),
      body: _loading
          ? const Center(child: CircularProgressIndicator())
          : _error != null
              ? _errorView()
              : _form(),
    );
  }

  Widget _errorView() {
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(24),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: <Widget>[
            const Icon(Icons.error_outline, color: Color(0xFFDD3333), size: 36),
            const SizedBox(height: 12),
            Text(_error!, textAlign: TextAlign.center),
            const SizedBox(height: 16),
            OutlinedButton(onPressed: _load, child: const Text('重试')),
          ],
        ),
      ),
    );
  }

  Widget _form() {
    return ListView(
      padding: const EdgeInsets.all(16),
      children: <Widget>[
        // ---------------- 头像 + 名称 ----------------
        Row(
          children: <Widget>[
            GestureDetector(onTap: _pickAvatar, child: _avatarPreview()),
            const SizedBox(width: 14),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: <Widget>[
                  const Text('对方头像',
                      style: TextStyle(fontSize: 14, color: Color(0xFF333333))),
                  const SizedBox(height: 4),
                  const Text('点左边头像从相册选一张',
                      style:
                          TextStyle(fontSize: 12, color: Color(0xFF999999))),
                  const SizedBox(height: 6),
                  OutlinedButton.icon(
                    onPressed: _pickAvatar,
                    icon: const Icon(Icons.photo_library_outlined, size: 18),
                    label: const Text('从相册选择'),
                  ),
                ],
              ),
            ),
          ],
        ),
        const SizedBox(height: 20),
        _textField(
          label: '对方名称',
          controller: _name,
          hint: '比如：老王',
          helper: '会话标题、聊天气泡旁边的名字都用它',
        ),

        // ---------------- API ----------------
        const SizedBox(height: 8),
        const Divider(),
        const SizedBox(height: 8),
        const Text('API 接口',
            style: TextStyle(
                fontSize: 15,
                fontWeight: FontWeight.w600,
                color: Color(0xFF222222))),
        const SizedBox(height: 6),
        const Text(
          '只作用于这一个聊天。没填就走全局 API 池；全局也没有时，'
          '发消息会明确提示“还没配置 API”，不会假装回复。',
          style: TextStyle(fontSize: 12, color: Color(0xFF999999)),
        ),
        const SizedBox(height: 12),
        const Text('厂家',
            style: TextStyle(fontSize: 14, color: Color(0xFF333333))),
        const SizedBox(height: 6),
        Wrap(
          spacing: 8,
          runSpacing: 8,
          children: <Widget>[
            for (final String p in _providers)
              ChoiceChip(
                label: Text(_providerLabels[p] ?? p),
                selected: _provider == p,
                onSelected: (_) => setState(() => _provider = p),
              ),
          ],
        ),
        if (_detected.isNotEmpty && _provider != 'auto') ...<Widget>[
          const SizedBox(height: 6),
          Text('服务端按 Key 识别出的厂家：$_detected',
              style: const TextStyle(fontSize: 12, color: Color(0xFF888888))),
        ],
        const SizedBox(height: 14),
        _textField(
          label: 'API 接口地址（Base URL）',
          controller: _baseUrl,
          hint: 'https://api.deepseek.com/v1',
          helper: '选 DeepSeek / OpenAI / GLM 时留空即可，服务端有内置地址',
          keyboardType: TextInputType.url,
        ),
        _textField(
          label: '模型名（可留空）',
          controller: _model,
          hint: 'deepseek-chat',
          helper: '留空则用厂家默认模型',
        ),
        _textField(
          label: 'API Key',
          controller: _apiKey,
          hint: _hasKey ? '已保存（尾号 $_keyTail），留空表示不改' : '粘贴你的 API Key',
          helper: '只存在你自己的电脑上，接口只回后四位，不回明文',
          obscure: true,
        ),
        if (_hasKey)
          Align(
            alignment: Alignment.centerLeft,
            child: TextButton.icon(
              onPressed: _clearKey,
              icon: const Icon(Icons.delete_outline, size: 18),
              label: Text('清除已保存的 Key（尾号 $_keyTail）'),
            ),
          ),

        // ---------------- 人格数据 ----------------
        const SizedBox(height: 8),
        const Divider(),
        const SizedBox(height: 8),
        const Text('人格对话数据',
            style: TextStyle(
                fontSize: 15,
                fontWeight: FontWeight.w600,
                color: Color(0xFF222222))),
        const SizedBox(height: 6),
        const Text(
          '这份人格的完整档案（JSON）。可以直接改数值，也可以整段换成别处生成的结果。',
          style: TextStyle(fontSize: 12, color: Color(0xFF999999)),
        ),
        const SizedBox(height: 10),
        TextField(
          controller: _personality,
          minLines: 8,
          maxLines: 18,
          style: const TextStyle(fontFamily: 'monospace', fontSize: 12),
          decoration: InputDecoration(
            hintText: '{\n  "tone": "casual",\n  ...\n}',
            hintStyle:
                const TextStyle(fontSize: 12, color: Color(0xFFBBBBBB)),
            isDense: true,
            contentPadding: const EdgeInsets.all(12),
            border: OutlineInputBorder(
              borderRadius: BorderRadius.circular(6),
              borderSide: const BorderSide(color: Color(0xFFDDDDDD)),
            ),
          ),
        ),

        // ---------------- 用聊天记录生成人格 ----------------
        //
        // 用户 2026-09-27 第七轮要求：人格要从**聊天记录原文**里学，
        // 而且要学到「朋友隔多久才回」「一句话分几条发」这种节奏。
        // 所以这里收的是一段文本，每行「说话人：内容」，
        // 行尾括号里写相对上一条的间隔（服务端 persona_text 解析）。
        const SizedBox(height: 16),
        const Text('用聊天记录生成人格（文本）',
            style: TextStyle(fontSize: 14, color: Color(0xFF333333))),
        const SizedBox(height: 4),
        const Text(
          '一行一句，写成「说话人：内容」。想让 AI 学到对方多久才回你，'
          '就在行尾括号里写上间隔，比如（3分钟后）（10秒后）。\n'
          '空行表示隔了一次发言，同一个人连着几行就是连发几条。',
          style: TextStyle(fontSize: 12, color: Color(0xFF999999)),
        ),
        const SizedBox(height: 8),
        TextField(
          controller: _chatLog,
          minLines: 5,
          maxLines: 12,
          style: const TextStyle(fontSize: 12),
          decoration: InputDecoration(
            hintText: '用户：兄弟你在干啥？\n'
                '朋友：我在外面骑车呢，你要来不（3分钟后）\n'
                '\n'
                '用户：你在哪里？我也要来（10秒后）\n'
                '朋友：我在公园（2秒后）\n'
                '朋友：这里捡到钱了，赶快过来（3秒后）',
            hintStyle: const TextStyle(fontSize: 12, color: Color(0xFFBBBBBB)),
            isDense: true,
            contentPadding: const EdgeInsets.all(12),
            border: OutlineInputBorder(
              borderRadius: BorderRadius.circular(6),
              borderSide: const BorderSide(color: Color(0xFFDDDDDD)),
            ),
          ),
        ),
        const SizedBox(height: 6),
        Row(
          children: <Widget>[
            SizedBox(
              width: 22,
              height: 22,
              child: Checkbox(
                value: _authorized,
                onChanged: (bool? v) =>
                    setState(() => _authorized = v ?? false),
              ),
            ),
            const SizedBox(width: 4),
            const Expanded(
              child: Text(
                '我确认已获得对方授权（PRD 5.2 要求）',
                style: TextStyle(fontSize: 12, color: Color(0xFF666666)),
              ),
            ),
          ],
        ),
        const SizedBox(height: 8),
        Row(
          mainAxisAlignment: MainAxisAlignment.center,
          children: <Widget>[
            SizedBox(
              width: 170,
              height: 40,
              child: OutlinedButton.icon(
                onPressed: _distilling ? null : _distillFromChatLog,
                icon: const Icon(Icons.auto_awesome, size: 18),
                label: Text(_distilling ? '生成中…' : '导入并生成人格'),
              ),
            ),
            const SizedBox(width: 10),
            SizedBox(
              width: 110,
              height: 40,
              child: OutlinedButton(
                onPressed: _importChatLogFile,
                child: const Text('从文件导入'),
              ),
            ),
          ],
        ),

        const SizedBox(height: 22),
        Center(
          child: SizedBox(
            width: 150,
            height: 40,
            child: ElevatedButton(
              onPressed: _saving ? null : _save,
              style: ElevatedButton.styleFrom(
                backgroundColor: const Color(0xFF0099FF),
                foregroundColor: Colors.white,
                padding: EdgeInsets.zero,
              ),
              child: Text(_saving ? '保存中…' : '保存'),
            ),
          ),
        ),

        // ---------------- 删除好友 ----------------
        // 用户 2026-09-27 第六轮要求：「再增加一个删除功能，随时可以删除这个 AI
        // 聊天好友，删除模块在每个好友聊天框点进去后的右上角设置」。
        // 单独放到最下面、用红色，避免和「保存」挨在一起被误点。
        const SizedBox(height: 34),
        const Divider(),
        const SizedBox(height: 14),
        Center(
          child: SizedBox(
            width: 170,
            height: 40,
            child: OutlinedButton(
              onPressed: _deleting ? null : _deleteFriend,
              style: OutlinedButton.styleFrom(
                foregroundColor: const Color(0xFFD32F2F),
                side: const BorderSide(color: Color(0xFFE57373)),
                padding: EdgeInsets.zero,
              ),
              child: Text(_deleting ? '删除中…' : '删除这位好友'),
            ),
          ),
        ),
        const SizedBox(height: 8),
        const Center(
          child: Text(
            '删除后聊天记录和人格一起清掉，不能恢复',
            style: TextStyle(fontSize: 12, color: Color(0xFF999999)),
          ),
        ),
        const SizedBox(height: 30),
      ],
    );
  }

  /// 删除这个好友：先二次确认（不可恢复，必须问一次），再调服务端。
  ///
  /// 服务端做的事（见 `DELETE /api/ai-profiles/{id}`）：
  /// 档案 / 人格 / 头像 / 单聊会话及其消息 / 延迟任务 / 记忆全部清掉；
  /// 如果它在某个群里，只把它从群成员里移走，群和别人说过的话都留着。
  Future<void> _deleteFriend() async {
    final String name = _name.text.trim();
    final bool? ok = await showDialog<bool>(
      context: context,
      builder: (BuildContext ctx) => AlertDialog(
        title: const Text('删除这位好友？'),
        content: Text(
          '「${name.isEmpty ? '这位好友' : name}」的档案、人格、头像和全部聊天记录'
          '都会被删掉，不能恢复。\n\n'
          '如果它在某个群聊里，会被移出那个群。',
        ),
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
    if (ok != true) return;

    setState(() => _deleting = true);
    try {
      await ApiClient.instance.deleteAiProfile(widget.aiProfileId);
      // 会话列表、消息缓存都要跟着刷新：这个人的单聊会话已经不存在了。
      await LocalQueue.instance.refreshConversations();
      if (!mounted) return;
      _toast('已删除');
      // 回 'deleted' 让聊天页知道“这个东西没了”，它会自己退回首页。
      Navigator.of(context).pop('deleted');
    } catch (e) {
      if (mounted) _toast('删除失败：$e');
    } finally {
      if (mounted) setState(() => _deleting = false);
    }
  }

  Widget _textField({
    required String label,
    required TextEditingController controller,
    required String hint,
    String? helper,
    TextInputType? keyboardType,
    bool obscure = false,
  }) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          Text(label,
              style: const TextStyle(fontSize: 14, color: Color(0xFF333333))),
          const SizedBox(height: 6),
          TextField(
            controller: controller,
            keyboardType: keyboardType,
            obscureText: obscure,
            decoration: InputDecoration(
              hintText: hint,
              helperText: helper,
              helperMaxLines: 2,
              isDense: true,
              contentPadding:
                  const EdgeInsets.symmetric(horizontal: 12, vertical: 12),
              border: OutlineInputBorder(
                borderRadius: BorderRadius.circular(6),
                borderSide: const BorderSide(color: Color(0xFFDDDDDD)),
              ),
            ),
          ),
        ],
      ),
    );
  }
}
