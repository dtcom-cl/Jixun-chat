// 设置页（用户 2026-09-27 第六轮：全局设置 = 连接 + 账号 + 桌面图标）。
//

import 'dart:async';

import 'package:flutter/material.dart';

import '../config/app_assets.dart';
import '../config/app_config.dart';
import '../services/api_client.dart';
import '../services/launcher_icon.dart';
import '../services/server_discovery.dart';
import '../services/session.dart';
import '../services/ws_client.dart';

class SettingsPage extends StatefulWidget {
  const SettingsPage({super.key});

  @override
  State<SettingsPage> createState() => _SettingsPageState();
}

class _SettingsPageState extends State<SettingsPage>
    with WidgetsBindingObserver {
  final AppConfig _cfg = AppConfig.instance;
  final WsClient _ws = WsClient.instance;

  late final TextEditingController _serverUrl;
  final FocusNode _serverFocus = FocusNode();

  late int _launcherIndex;
  bool _busy = false;

  /// 上一次已落盘的地址（规范化后），避免重复写盘。
  late String _savedUrl;

  /// 键盘从开到关后延迟落盘的定时器。
  Timer? _blurSaveTimer;

  /// 键盘上一帧是不是开着的。
  bool _keyboardWasOpen = false;

  /// 刚存过地址时为 true，亮 2 秒「地址已保存」。
  bool _urlSaved = false;

  /// 正在广播寻找电脑。
  bool _discovering = false;

  /// 自动寻找服务器：广播 -> 谁应答就用谁，**真连一下确认能通**，再保存并重连。
  ///
  /// 2026-10-01 第二十八轮（用户「找找其他的办法」）：电脑连手机热点时，
  /// 手机和电脑本来就在同一个网络里，但电脑 IP 会变；这个按钮让 App 自己找到它。
  ///
  /// 2026-10-02 第二十九轮（用户「这个功能目前根本没用」）：
  /// 只「找到」不够 —— 电脑可能有多张网卡、地址可能过期。
  /// 所以现在把候选地址挨个真连一遍，只采用能连上的那条；全都连不上就照实说。
  ///
  /// 2026-10-02 第二十九轮补（用户「自动搜索服务器还是没有用，搜不到的」）：
  /// 广播本身可能发不出去（手机自己做热点时，默认路由是流量网卡），
  /// 所以 `ServerDiscovery.find()` 里加了兜底：**按本机网段挨个敲 8000 端口**。
  /// 只走局域网 IPv4（用户明确说「没必要再用 IPv6」）。
  Future<void> _autoFindServer() async {
    setState(() => _discovering = true);
    try {
      final int knownPort =
          Uri.tryParse(_cfg.serverUrl)?.port ?? ServerDiscovery.defaultPort;
      final String? knownHost = Uri.tryParse(_cfg.serverUrl)?.host;
      final List<DiscoveredServer> found = await ServerDiscovery.find(
        knownPort: knownPort,
        knownHost: (knownHost != null && knownHost.isNotEmpty) ? knownHost : null,
      );
      if (!mounted) return;
      if (found.isEmpty) {
        _toast('没找到服务器。确认：① 电脑上 start.bat 开着 ② 手机和电脑在同一个 WiFi 或同一个手机热点下'
            '（广播探测和按网段扫描都没找到）');
        return;
      }
      final List<String> tried = <String>[];
      String? working;
      DiscoveredServer? matched;

      // 2026-10-02（用户改用 Cloudflare Tunnel）：电脑在应答里带了「当前云端地址」时
      // 顺手存下来 —— 出门（局域网连不上）时 WsClient 会自动切过去，
      // 用户完全不用管「隧道网址每次重启都会变」这件事。
      for (final DiscoveredServer s in found) {
        final String c = (s.cloudUrl ?? '').trim();
        if (c.isNotEmpty) {
          if (c != _cfg.cloudUrl) await _cfg.update(cloudUrl: c);
          break;
        }
      }
      for (final DiscoveredServer s in found) {
        for (final String url in s.candidates) {
          if (tried.contains(url)) continue;
          tried.add(url);
          if (await ServerDiscovery.probe(url)) {
            working = url;
            matched = s;
            break;
          }
        }
        if (working != null) break;
      }
      if (!mounted) return;
      if (working == null) {
        // 一个都连不上：还是把第一台填上（方便用户自己改），但把话说清楚，
        // 不能像以前那样只报「找到了」让人以为好了。
        final DiscoveredServer server = found.first;
        _serverUrl.text = server.wsUrl;
        await _saveUrl();
        await _reconnect();
        _toast('找到「${server.name}」，但 ${tried.length} 个地址都连不上。确认电脑上 start.bat 还开着、没被防火墙拦');
        return;
      }
      final DiscoveredServer server = matched ?? found.first;
      _serverUrl.text = working;
      await _saveUrl();
      final String extra = found.length > 1 ? '找到 ${found.length} 台服务器，' : '';
      _toast('$extra已连上「${server.name}」：$working');
      await _reconnect();
    } finally {
      if (mounted) setState(() => _discovering = false);
    }
  }

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _serverUrl = TextEditingController(text: _cfg.serverUrl);
    _savedUrl = _cfg.serverUrl;
    _launcherIndex = _cfg.launcherIndex;
    _serverFocus.addListener(_onFocusChanged);
  }

  @override
  void dispose() {
    _blurSaveTimer?.cancel();
    WidgetsBinding.instance.removeObserver(this);
    _serverFocus.removeListener(_onFocusChanged);
    _serverFocus.dispose();
    _serverUrl.dispose();
    super.dispose();
  }

  void _toast(String text) {
    if (!mounted) return;
    ScaffoldMessenger.of(context)
      ..hideCurrentSnackBar()
      ..showSnackBar(SnackBar(content: Text(text)));
  }

  // ------------------------------------------------------------ 服务器地址

  /// 键盘是否可见：底部被输入法顶起来即为开着。
  ///
  /// 外面套 try：组件测试里没有真正的 View，View.of 会抛错，
  /// 那种环境当作「键盘没开」即可，不影响真实设备上的行为。
  bool _keyboardVisible() {
    try {
      return View.of(context).viewInsets.bottom > 0;
    } catch (_) {
      return false;
    }
  }

  /// 输入法刚关闭 -> 稍等一下落盘（用户 2026-09-27 明确要的行为）。
  ///
  /// 为什么不能只靠 FocusNode：部分中文输入法收起键盘时不会主动交还焦点，
  /// 失焦回调根本不触发，用户就以为「地址没法保存」。
  @override
  void didChangeMetrics() {
    super.didChangeMetrics();
    if (!mounted) return;
    final bool open = _keyboardVisible();
    if (_keyboardWasOpen && !open) _scheduleSave();
    _keyboardWasOpen = open;
  }

  void _scheduleSave() {
    _blurSaveTimer?.cancel();
    _blurSaveTimer = Timer(const Duration(milliseconds: 120), _saveUrlSilently);
  }

  /// 输入变化 + 800ms 无新按键 -> 顺手存一次（连键盘都没碰也不丢）。
  void _onServerTextChanged(String _) {
    _blurSaveTimer?.cancel();
    _blurSaveTimer =
        Timer(const Duration(milliseconds: 800), _saveUrlSilently);
  }

  /// 输入框失焦就自动保存，不用再点按钮。
  void _onFocusChanged() {
    if (!_serverFocus.hasFocus) _saveUrl(silent: true);
  }

  /// 静默保存：地址非法（或没变化）就什么都不做，绝不弹错误打断输入。
  void _saveUrlSilently() {
    final String raw = _serverUrl.text.trim();
    if (raw.isEmpty) return;
    if (AppConfig.validateServerUrl(raw) != null) return;
    if (AppConfig.normalizeServerUrl(raw) == _savedUrl) return;
    unawaited(_saveUrl(silent: true));
  }

  /// 只保存地址，不动连接。
  Future<void> _saveUrl({bool silent = false}) async {
    final String? err = AppConfig.validateServerUrl(_serverUrl.text);
    if (err != null) {
      _toast(err);
      return;
    }
    await _cfg.update(serverUrl: _serverUrl.text);
    if (!mounted) return;
    // 回填规范化后的地址，让用户看到真正生效的值。
    _serverUrl.text = _cfg.serverUrl;
    _savedUrl = _cfg.serverUrl;
    setState(() => _urlSaved = true);
    Timer(const Duration(seconds: 2), () {
      if (mounted) setState(() => _urlSaved = false);
    });
    if (!silent) _toast('服务器地址已保存');
  }

  /// 「重连」：先落盘再强制重连。
  Future<void> _reconnect() async {
    final String? err = AppConfig.validateServerUrl(_serverUrl.text);
    if (err != null) {
      _toast(err);
      return;
    }
    setState(() => _busy = true);
    try {
      await _cfg.update(serverUrl: _serverUrl.text);
      if (!mounted) return;
      _serverUrl.text = _cfg.serverUrl;
      _ws.connect(force: true);
      _toast('正在连接 ${_cfg.serverUrl}');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  // ------------------------------------------------------------ 账号（第六轮）

  /// 账号卡片：显示账号 ID 与昵称。
  ///
  /// 账号 ID 必须显眼 —— 用户要「换手机重新登录」时，能凭的就是这 4 位数字。
  Widget _accountCard() {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 12),
      decoration: BoxDecoration(
        color: const Color(0xFFF7F8FA),
        borderRadius: BorderRadius.circular(6),
        border: Border.all(color: const Color(0xFFE6E8EB)),
      ),
      child: Row(
        children: <Widget>[
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: <Widget>[
                Text(
                  '账号：${_cfg.accountId.isEmpty ? '—' : _cfg.accountId}',
                  style: const TextStyle(
                    fontSize: 14,
                    color: Color(0xFF333333),
                    fontWeight: FontWeight.w600,
                  ),
                ),
                const SizedBox(height: 4),
                Text(
                  '昵称：${_cfg.userName.isEmpty ? '—' : _cfg.userName}',
                  style: const TextStyle(fontSize: 12, color: Color(0xFF888888)),
                ),
              ],
            ),
          ),
          const Icon(Icons.badge_outlined, size: 20, color: Color(0xFFB0B7C3)),
        ],
      ),
    );
  }

  /// 修改密码：弹一个对话框收旧密码 + 新密码（+ 确认），成功后服务端令牌不变。
  Future<void> _changePassword() async {
    final TextEditingController oldPwd = TextEditingController();
    final TextEditingController newPwd = TextEditingController();
    final TextEditingController confirm = TextEditingController();

    final bool? ok = await showDialog<bool>(
      context: context,
      builder: (BuildContext ctx) => AlertDialog(
        title: const Text('修改密码'),
        content: Column(
          mainAxisSize: MainAxisSize.min,
          children: <Widget>[
            TextField(
              controller: oldPwd,
              obscureText: true,
              decoration: const InputDecoration(labelText: '原密码'),
            ),
            TextField(
              controller: newPwd,
              obscureText: true,
              decoration: const InputDecoration(
                labelText: '新密码',
                helperText: '至少 6 位，含字母和数字',
              ),
            ),
            TextField(
              controller: confirm,
              obscureText: true,
              decoration: const InputDecoration(labelText: '再输一次新密码'),
            ),
          ],
        ),
        actions: <Widget>[
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(false),
            child: const Text('取消'),
          ),
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(true),
            child: const Text('确定'),
          ),
        ],
      ),
    );
    // 对话框关掉之后就不能再读 controller 了，先把值取出来再 dispose。
    final String o = oldPwd.text;
    final String n = newPwd.text;
    final String again = confirm.text;
    oldPwd.dispose();
    newPwd.dispose();
    confirm.dispose();

    if (ok != true) return;
    if (n != again) {
      _toast('两次输入的新密码不一样');
      return;
    }
    final String? weak = AppConfig.validatePassword(n);
    if (weak != null) {
      _toast(weak);
      return;
    }

    setState(() => _busy = true);
    try {
      await ApiClient.instance.changePassword(oldPassword: o, newPassword: n);
      if (mounted) _toast('密码已修改');
    } on ApiException catch (e) {
      if (mounted) _toast('修改失败：${e.message}');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  /// 退出登录：先让服务端忘掉令牌，再清本地凭证，首屏会自动切回登录页。
  Future<void> _logout() async {
    final bool? ok = await showDialog<bool>(
      context: context,
      builder: (BuildContext ctx) => AlertDialog(
        title: const Text('退出登录？'),
        content: const Text(
          '退出后需要重新输入账号和密码才能进来。\n'
          '你的好友、人格、API 配置都在电脑服务端上，不会丢。',
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
            child: const Text('退出'),
          ),
        ],
      ),
    );
    if (ok != true || !mounted) return;

    setState(() => _busy = true);
    try {
      await Session.instance.logout();
      if (!mounted) return;
      // 首屏由 main.dart 的 _RootGate 按 isLoggedIn 分流，会自动换成注册/登录页。
      Navigator.of(context).pop();
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  // ------------------------------------------------------------ 桌面图标

  Future<void> _applyLauncher() async {
    final String label = launcherIdentityByIndex(_launcherIndex).label;
    // 2026-10-01 第二十轮（用户实测「切换后闪退，然后图标变成了微信但名字还是 QQ，
    // 再进去图标又变回企鹅」）：
    // 切换 activity-alias 的启用状态在部分 ROM 上会**把应用进程杀掉**，
    // 所以顺序必须变成「先把选择落盘 + 传给服务端，最后才切图标」——
    // 被杀也不会丢掉用户的选择，下次启动读到的就是微信那一套。
    await _cfg.update(launcherIndex: _launcherIndex, appName: label);
    try {
      await ApiClient.instance
          .updateMe(launcherIndex: _launcherIndex, appName: label);
    } on ApiException {
      // 上传失败不影响本机已经切好的图标。
    }
    // 2026-10-01 第二十一轮：**已经是这一套就不要再点原生那一下** ——
    // 切换 alias 在部分 ROM 上会杀进程，用户连点两次同一项不该被重启两次。
    final int current = await LauncherIcon.current();
    final bool ok =
        current == _launcherIndex ? true : await LauncherIcon.apply(_launcherIndex);
    if (!mounted) return;
    if (ok) {
      _toast('已尝试切换');
    } else {
      _toast('当前平台不支持运行时切换桌面图标（Android 真机可用）');
    }
  }

  // ------------------------------------------------------------ UI

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        elevation: 0,
        backgroundColor: const Color(0xFFF7F7F7),
        title: const Text('设置', style: TextStyle(fontSize: 16)),
      ),
      body: ListView(
        padding: const EdgeInsets.fromLTRB(16, 8, 16, 24),
        children: <Widget>[
          _sectionTitle('连接'),
          TextField(
            controller: _serverUrl,
            focusNode: _serverFocus,
            keyboardType: TextInputType.url,
            onChanged: _onServerTextChanged,
            onSubmitted: (_) => _saveUrl(),
            decoration: InputDecoration(
              // 2026-10-01 第二十二轮：广域网用隧道/域名时是不带端口的 wss 地址
              hintText: 'ws://192.168.x.x:8000/ws | wss://域名/ws',
              isDense: true,
              contentPadding:
                  const EdgeInsets.symmetric(horizontal: 12, vertical: 12),
              border: OutlineInputBorder(
                borderRadius: BorderRadius.circular(6),
                borderSide: const BorderSide(color: Color(0xFFDDDDDD)),
              ),
            ),
          ),
          // 保存回执：高度固定，出现/消失都不会把下面的按钮顶下去。
          SizedBox(
            height: 18,
            child: Align(
              alignment: Alignment.centerLeft,
              child: _urlSaved
                  ? const Text(
                      '地址已保存',
                      style: TextStyle(fontSize: 12, color: Color(0xFF33AA55)),
                    )
                  : const SizedBox.shrink(),
            ),
          ),
          const SizedBox(height: 4),
          // 2026-10-01 第二十八轮：自动找到电脑。
          //
          // 用户场景：「电脑连手机热点、手机用这个 App」，这时手机和电脑本来就在同一个
          // 网络里（热点网段），局域网直连一直是好的；但电脑 IP 会变（换网络、重连热点），
          // 每次都得手改地址 —— 这个按钮就是干这个的：广播一下让电脑自己报地址。
          // 纯本地广播，不经过任何第三方、不需要账号。
          Align(
            alignment: Alignment.centerLeft,
            child: TextButton.icon(
              onPressed: _busy || _discovering ? null : _autoFindServer,
              icon: _discovering
                  ? const SizedBox(
                      width: 14,
                      height: 14,
                      child: CircularProgressIndicator(strokeWidth: 2),
                    )
                  : const Icon(Icons.wifi_find, size: 18),
              label: Text(
                _discovering ? '正在寻找…' : '自动寻找服务器',
                style: const TextStyle(fontSize: 13),
              ),
            ),
          ),
          Center(
            child: SizedBox(
              width: 132,
              height: 40,
              child: ElevatedButton(
                onPressed: _busy ? null : _reconnect,
                style: ElevatedButton.styleFrom(
                  backgroundColor: const Color(0xFF0099FF),
                  foregroundColor: Colors.white,
                  elevation: 0,
                ),
                child: const Text('重连'),
              ),
            ),
          ),
          const SizedBox(height: 28),
          _sectionTitle('账号'),
          _accountCard(),
          const SizedBox(height: 12),
          Center(
            child: SizedBox(
              width: 150,
              height: 40,
              child: OutlinedButton(
                onPressed: _busy ? null : _changePassword,
                child: const Text('修改密码'),
              ),
            ),
          ),
          const SizedBox(height: 10),
          Center(
            child: SizedBox(
              width: 150,
              height: 40,
              child: OutlinedButton(
                onPressed: _busy ? null : _logout,
                style: OutlinedButton.styleFrom(
                  foregroundColor: const Color(0xFFD32F2F),
                  side: const BorderSide(color: Color(0xFFE57373)),
                ),
                child: const Text('退出登录'),
              ),
            ),
          ),
          const SizedBox(height: 28),
          _sectionTitle('手机桌面图标与名称'),
          _launcherPicker(),
          const SizedBox(height: 12),
          Center(
            child: SizedBox(
              width: 150,
              height: 40,
              child: OutlinedButton(
                onPressed: _applyLauncher,
                child: const Text('应用到手机桌面'),
              ),
            ),
          ),
        ],
      ),
    );
  }

  Widget _sectionTitle(String text) => Padding(
        padding: const EdgeInsets.only(bottom: 10),
        child: Text(
          text,
          style: const TextStyle(
            fontSize: 14,
            fontWeight: FontWeight.w600,
            color: Color(0xFF333333),
          ),
        ),
      );

  Widget _launcherPicker() {
    return Wrap(
      spacing: 8,
      runSpacing: 8,
      children: <Widget>[
        for (final LauncherIdentity id in kLauncherIdentities)
          ChoiceChip(
            label: Text(id.label),
            selected: id.aliasIndex == _launcherIndex,
            onSelected: (_) => setState(() => _launcherIndex = id.aliasIndex),
          ),
      ],
    );
  }
}
