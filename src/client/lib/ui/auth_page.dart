// 注册 / 登录页（用户 2026-09-27 第六轮要求）。
//

import 'dart:async';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../config/app_config.dart';
import '../services/image_picker_service.dart';
import '../services/session.dart';

class AuthPage extends StatefulWidget {
  const AuthPage({super.key});

  @override
  State<AuthPage> createState() => _AuthPageState();
}

class _AuthPageState extends State<AuthPage> with WidgetsBindingObserver {
  final Session _session = Session.instance;
  final AppConfig _cfg = AppConfig.instance;

  /// true = 登录形态，false = 注册形态。默认注册（用户：「目前第1次使用叫做注册」）。
  bool _isLogin = false;

  final TextEditingController _nickname = TextEditingController();
  final TextEditingController _password = TextEditingController();
  final TextEditingController _accountId = TextEditingController();
  final FocusNode _serverFocus = FocusNode();
  late final TextEditingController _serverUrl;

  /// 注册时刚选的头像（还没上传）。
  Uint8List? _pendingAvatar;

  /// 服务器地址输入框是否展开（默认收起，避免第一屏被技术细节占满）。
  bool _showServer = false;

  /// 上一次已经落盘的地址（规范化之后）。用来判断「这次改动是否真的需要写盘」，
  /// 避免每敲一个字符都写一次文件。
  late String _savedUrl;

  /// 键盘（输入法）关闭后延迟一点点再落盘的定时器。
  ///
  /// 为什么要延迟：关键盘的瞬间，焦点可能还没真正交出去，
  /// 立刻读 _serverUrl.text 有概率读到抖动中的中间态；
  /// 等 120ms 让 Flutter 把这一帧走完，再去保存更稳。
  Timer? _blurSaveTimer;

  /// 键盘此刻是不是开着的。用来识别「从开到关」这个动作。
  bool _keyboardWasOpen = false;

  /// 刚存过地址时为 true，用来在输入框旁边亮 2 秒「已保存」。
  bool _urlSaved = false;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _serverUrl = TextEditingController(text: _cfg.serverUrl);
    _savedUrl = _cfg.serverUrl;
    _serverFocus.addListener(_onServerBlur);
    _session.addListener(_onSessionChanged);
  }

  @override
  void dispose() {
    _blurSaveTimer?.cancel();
    WidgetsBinding.instance.removeObserver(this);
    _session.removeListener(_onSessionChanged);
    _serverFocus.removeListener(_onServerBlur);
    _nickname.dispose();
    _password.dispose();
    _accountId.dispose();
    _serverUrl.dispose();
    _serverFocus.dispose();
    super.dispose();
  }

  void _onSessionChanged() {
    if (mounted) setState(() {});
  }

  // ------------------------------------------------------------ 地址保存
  //
  // 用户 2026-09-27 反馈：「服务器配置中的地址是不是没法保存啊？这样保存，
  // 如果检测到输入法关闭就立即保存配置」。
  //
  // 原来的实现只有 FocusNode 失焦 + 点按钮两条路，两个都不可靠：
  //   · 有些中文输入法在收起键盘时并不主动把焦点交出去，_onServerBlur 不触发；
  //   · 收起状态的那行「服务器配置」只是一个按钮，用户改完地址没点它，
  //     界面上也看不出到底存没存，主观感受就是「没法保存」。
  //
  // 现在补三条互相兜底的路，任一触发都能落盘：
  //   1. 失焦（FocusNode）—— 老路，留着；
  //   2. 键盘从开变关（didChangeMetrics 算出来的 viewInsets.bottom）—— 用户要的这条；
  //   3. 输入内容变化 + 800ms 无新输入（防抖）—— 连键盘都不碰的极端情况。

  /// 键盘是否可见：底部被输入法顶起来即为开着。
  ///
  /// 外面套 try 是因为组件测试（flutter test）里没有真正的 View，
  /// View.of 会抛错；那种环境下直接当作「键盘没开」，不影响任何逻辑。
  bool _keyboardVisible() {
    try {
      return View.of(context).viewInsets.bottom > 0;
    } catch (_) {
      return false;
    }
  }

  @override
  void didChangeMetrics() {
    super.didChangeMetrics();
    if (!mounted) return;
    final bool open = _keyboardVisible();
    if (_keyboardWasOpen && !open) {
      // 输入法刚刚关闭 -> 稍等一下再保存（用户明确要的行为）。
      _scheduleSave();
    }
    _keyboardWasOpen = open;
  }

  void _scheduleSave() {
    _blurSaveTimer?.cancel();
    _blurSaveTimer = Timer(const Duration(milliseconds: 120), _saveServerUrl);
  }

  /// 输入变化：800ms 内没有新按键就顺手存一次（防抖）。
  void _onServerTextChanged(String _) {
    _blurSaveTimer?.cancel();
    _blurSaveTimer =
        Timer(const Duration(milliseconds: 800), _saveServerUrl);
  }

  /// 服务器地址失焦即落盘（用户第五轮就要求「失焦自动保存」，这里沿用）。
  void _onServerBlur() {
    if (_serverFocus.hasFocus) return;
    _saveServerUrl();
  }

  /// 真正的落盘动作。地址不合法就**什么都不做**（不覆盖已存的好地址）。
  ///
  /// 返回值：true = 这次真的写进去了，false = 没写（空 / 非法 / 没变化）。
  bool _saveServerUrl() {
    final String raw = _serverUrl.text.trim();
    if (raw.isEmpty) return false;
    if (AppConfig.validateServerUrl(raw) != null) return false;
    final String normalized = AppConfig.normalizeServerUrl(raw);
    if (normalized == _savedUrl) return false;
    _savedUrl = normalized;
    // update() 内部还会再 normalize 一次，并把结果写进 settings.json。
    unawaited(_cfg.update(serverUrl: raw));
    // 用户反馈「看不出到底存没存」，所以给一个短暂的「已保存」回执。
    if (mounted) {
      setState(() => _urlSaved = true);
      Timer(const Duration(seconds: 2), () {
        if (mounted) setState(() => _urlSaved = false);
      });
    }
    return true;
  }

  void _toast(String text) {
    if (!mounted) return;
    ScaffoldMessenger.of(context)
      ..hideCurrentSnackBar()
      ..showSnackBar(SnackBar(
        content: Text(text),
        duration: const Duration(seconds: 4),
      ));
  }

  // ------------------------------------------------------------ 选头像

  Future<void> _pickAvatar() async {
    final PickResult r = await ImagePickerService.pickFromGallery();
    if (!mounted) return;
    if (r.unsupported) {
      _toast('当前平台不支持打开相册，请在 Android 真机上使用');
      return;
    }
    if (r.cancelled) return;
    if (!r.isOk) {
      _toast('选头像失败：${r.error ?? '未知错误'}');
      return;
    }
    setState(() => _pendingAvatar = r.image!.bytes);
  }

  // ------------------------------------------------------------ 提交

  Future<void> _submit() async {
    // 服务器地址先落盘：不存下来，注册/登录连请求都发不出去。
    final String rawUrl = _serverUrl.text.trim();
    final String? urlErr = AppConfig.validateServerUrl(rawUrl);
    if (urlErr != null) {
      setState(() => _showServer = true);
      _toast(urlErr);
      return;
    }
    await _cfg.update(serverUrl: rawUrl);
    _savedUrl = _cfg.serverUrl;

    String? err;
    if (_isLogin) {
      err = await _session.login(
        accountId: _accountId.text,
        password: _password.text,
      );
    } else {
      if (_pendingAvatar == null && AppConfig.galleryPickerAvailable) {
        _toast('请先从相册选一张头像');
        return;
      }
      err = await _session.register(
        nickname: _nickname.text,
        password: _password.text,
        avatarBytes: _pendingAvatar,
      );
    }
    if (!mounted) return;
    if (err != null) {
      _toast(err);
      return;
    }
    // 成功后不需要在这里跳转：main.dart 的首屏按 isLoggedIn 分流，
    // Session.notifyListeners() 会让外层重建，自动换成消息列表页。
    _toast(_isLogin ? '登录成功' : '注册成功，你的账号是 ${_cfg.accountId}');
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
        automaticallyImplyLeading: false,
        title: Text(_isLogin ? '登录' : '注册', style: const TextStyle(fontSize: 16)),
      ),
      body: ListView(
        padding: const EdgeInsets.fromLTRB(20, 18, 20, 32),
        children: <Widget>[
          if (!_isLogin) ...<Widget>[
            const SizedBox(height: 6),
            Center(child: _avatarPicker()),
            const SizedBox(height: 24),
          ],
          if (_isLogin) ...<Widget>[
            const SizedBox(height: 24),
            _field(
              label: '账号',
              controller: _accountId,
              hint: '4 位数字，例如 0001',
              keyboardType: TextInputType.number,
              formatters: <TextInputFormatter>[
                FilteringTextInputFormatter.digitsOnly,
                LengthLimitingTextInputFormatter(4),
              ],
            ),
          ] else ...<Widget>[
            _field(
              label: '昵称',
              controller: _nickname,
              hint: '比如：阿伟',
              formatters: <TextInputFormatter>[_UserNameLimiter()],
            ),
          ],
          _field(
            label: '密码',
            controller: _password,
            hint: _isLogin ? '请输入密码' : '至少 6 位，含字母和数字',
            obscure: true,
            onSubmitted: (_) => _submit(),
          ),

          // ---------------- 服务器地址 ----------------
          _serverSection(),

          const SizedBox(height: 18),
          Center(
            child: SizedBox(
              width: 150,
              height: 42,
              child: ElevatedButton(
                onPressed: (_session.busy || _session.cooldownSeconds > 0)
                    ? null
                    : _submit,
                style: ElevatedButton.styleFrom(
                  backgroundColor: const Color(0xFF0099FF),
                  foregroundColor: Colors.white,
                  disabledBackgroundColor: const Color(0xFFBBDDFF),
                  disabledForegroundColor: Colors.white,
                  elevation: 0,
                  padding: EdgeInsets.zero,
                  shape: RoundedRectangleBorder(
                    borderRadius: BorderRadius.circular(6),
                  ),
                ),
                child: Text(_buttonLabel()),
              ),
            ),
          ),
          const SizedBox(height: 14),
          Center(
            child: TextButton(
              onPressed: _session.busy
                  ? null
                  : () => setState(() {
                        _isLogin = !_isLogin;
                        _password.clear();
                      }),
              child: Text(_isLogin ? '没有账号？去注册' : '已有账号？去登录'),
            ),
          ),
          if (!_isLogin && !AppConfig.galleryPickerAvailable) ...<Widget>[
            const SizedBox(height: 4),
            const Center(
              child: Text(
                '当前平台打不开系统相册，可先不选头像',
                style: TextStyle(fontSize: 12, color: Color(0xFF999999)),
              ),
            ),
          ],
        ],
      ),
    );
  }

  String _buttonLabel() {
    if (_session.busy) return _isLogin ? '登录中…' : '注册中…';
    if (_session.cooldownSeconds > 0) {
      return '${_session.cooldownSeconds} 秒后可重试';
    }
    return _isLogin ? '登录' : '注册';
  }

  /// 服务器地址：默认收起成一行小字，点一下才展开。
  ///
  /// 为什么不直接摊开：这是给用户自己看的 App，第一屏应该是头像/昵称/密码，
  /// 而不是一个 IP 输入框。地址失焦即保存，收起来也不影响后续使用。
  Widget _serverSection() {
    if (!_showServer) {
      return Padding(
        padding: const EdgeInsets.only(top: 4),
        child: Align(
          alignment: Alignment.centerLeft,
          child: TextButton(
            onPressed: () => setState(() => _showServer = true),
            style: TextButton.styleFrom(
              padding: EdgeInsets.zero,
              minimumSize: const Size(0, 32),
              tapTargetSize: MaterialTapTargetSize.shrinkWrap,
            ),
            child: const Text(
              '服务器配置',
              style: TextStyle(fontSize: 12, color: Color(0xFF888888)),
            ),
          ),
        ),
      );
    }
    return Padding(
      padding: const EdgeInsets.only(top: 4),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          _field(
            label: '服务器地址',
            controller: _serverUrl,
            hint: 'ws://192.168.x.x:8000/ws',
            focusNode: _serverFocus,
            keyboardType: TextInputType.url,
            onChanged: _onServerTextChanged,
            onSubmitted: (_) => _saveServerUrl(),
          ),
          // 用户反馈「看不出到底存没存」，所以给一个明确回执。
          // 高度写死 16：无论文字出没出现，下面的按钮都不会被顶下去。
          SizedBox(
            height: 16,
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
        ],
      ),
    );
  }

  Widget _field({
    required String label,
    required TextEditingController controller,
    required String hint,
    bool obscure = false,
    FocusNode? focusNode,
    TextInputType? keyboardType,
    List<TextInputFormatter>? formatters,
    ValueChanged<String>? onSubmitted,
    ValueChanged<String>? onChanged,
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
            focusNode: focusNode,
            obscureText: obscure,
            keyboardType: keyboardType,
            inputFormatters: formatters,
            onSubmitted: onSubmitted,
            onChanged: onChanged,
            decoration: InputDecoration(
              hintText: hint,
              hintStyle: const TextStyle(fontSize: 13, color: Color(0xFFBBBBBB)),
              isDense: true,
              contentPadding:
                  const EdgeInsets.symmetric(horizontal: 12, vertical: 14),
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

  /// 圆形头像选择器（只用于注册形态）。
  Widget _avatarPicker() {
    const double size = 96;
    final Uint8List? pending = _pendingAvatar;

    Widget inner;
    if (pending != null) {
      inner = Image.memory(pending,
          width: size, height: size, fit: BoxFit.cover);
    } else if (_cfg.hasUserAvatar) {
      inner = Image.file(
        File(_cfg.userAvatarFile),
        width: size,
        height: size,
        fit: BoxFit.cover,
        errorBuilder: (_, _, _) => _placeholder(),
      );
    } else {
      inner = _placeholder();
    }

    return GestureDetector(
      onTap: _pickAvatar,
      child: Stack(
        children: <Widget>[
          Container(
            width: size,
            height: size,
            decoration: BoxDecoration(
              color: const Color(0xFFF0F2F5),
              shape: BoxShape.circle,
              border: Border.all(color: const Color(0xFFE0E0E0)),
            ),
            child: ClipOval(child: inner),
          ),
          Positioned(
            right: 0,
            bottom: 0,
            child: Container(
              padding: const EdgeInsets.all(5),
              decoration: const BoxDecoration(
                color: Color(0xFF0099FF),
                shape: BoxShape.circle,
              ),
              child: const Icon(Icons.photo_camera,
                  size: 15, color: Colors.white),
            ),
          ),
        ],
      ),
    );
  }

  Widget _placeholder() {
    return const Center(
      child: Icon(Icons.person, size: 46, color: Color(0xFFB9C2CF)),
    );
  }
}

/// 昵称长度限制：按字符（rune）计数，emoji 不会被截成半个。
class _UserNameLimiter extends TextInputFormatter {
  @override
  TextEditingValue formatEditUpdate(
    TextEditingValue oldValue,
    TextEditingValue newValue,
  ) {
    final List<int> runes = newValue.text.runes.toList();
    if (runes.length <= AppConfig.kMaxUserNameLength) return newValue;
    final String clipped =
        String.fromCharCodes(runes.take(AppConfig.kMaxUserNameLength));
    return TextEditingValue(
      text: clipped,
      selection: TextSelection.collapsed(offset: clipped.length),
    );
  }
}