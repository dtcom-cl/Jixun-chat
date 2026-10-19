// 个人资料页（用户 2026-09-27 要求；第六轮改为写服务端）。
//

import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';
// Uint8List 由 services.dart 提供（它 re-export 了 dart:typed_data 的相关部分），
// 所以这里不需要再单独 import dart:typed_data，否则 analyze 会报多余 import。
import 'package:flutter/services.dart';

import '../config/app_config.dart';
import '../services/api_client.dart';
import '../services/avatar_store.dart';
import '../services/image_picker_service.dart';

class ProfilePage extends StatefulWidget {
  const ProfilePage({super.key, this.isFirstRun = false});

  /// 是否是“首次安装引导”：为 true 时不显示返回按钮，必须填完才能继续。
  final bool isFirstRun;

  @override
  State<ProfilePage> createState() => _ProfilePageState();
}

class _ProfilePageState extends State<ProfilePage> {
  final AppConfig _cfg = AppConfig.instance;

  late TextEditingController _name;
  bool _busy = false;

  /// 本次是否已经选好了一张新头像（还没落盘）。
  ///
  /// 这样“先选头像再改昵称再点保存”只需要写一次文件，
  /// 中途退出也不会留下半套配置。
  Uint8List? _pendingAvatar;
  String _pendingAvatarKb = '';

  @override
  void initState() {
    super.initState();
    _name = TextEditingController(text: _cfg.userName);
  }

  @override
  void dispose() {
    _name.dispose();
    super.dispose();
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

  // ------------------------------------------------------------ 选头像

  /// 弹出系统相册让用户选一张头像（用户 2026-09-27 明确要求）。
  Future<void> _pickAvatar() async {
    if (_busy) return;
    final PickResult r = await ImagePickerService.pickFromGallery();
    if (!mounted) return;
    if (r.unsupported) {
      _toast('当前平台不支持打开相册，请在 Android 真机上使用');
      return;
    }
    if (r.cancelled) return; // 用户点了取消，静默返回
    if (!r.isOk) {
      _toast('选头像失败：${r.error ?? '未知错误'}');
      return;
    }
    final PickedImage img = r.image!;
    setState(() {
      _pendingAvatar = img.bytes;
      _pendingAvatarKb = img.sizeKb.toStringAsFixed(0);
    });
  }

  // ------------------------------------------------------------ 保存

  Future<void> _save() async {
    final String raw = _name.text;
    final String? err = AppConfig.validateUserName(raw);
    if (err != null) {
      _toast(err);
      return;
    }
    setState(() => _busy = true);
    try {
      final Uint8List? bytes = _pendingAvatar;
      String? cachedPath;
      if (bytes != null) {
        cachedPath = await AvatarStore.saveUser(bytes);
        if (cachedPath == null) {
          if (mounted) _toast('头像保存失败，换一张试试');
          return;
        }
      }

      try {
        await ApiClient.instance.updateMe(
          nickname: AppConfig.sanitizeUserName(raw),
          avatar: bytes == null ? null : base64Encode(bytes),
          avatarExt: 'jpg',
        );
      } on ApiException catch (e) {
        if (mounted) _toast('保存失败：${e.message}');
        return;
      }

      await _cfg.update(
        userName: AppConfig.sanitizeUserName(raw),
        userAvatarFile: cachedPath,
      );

      if (!mounted) return;
      _toast('已保存');
      Navigator.of(context).pop(true);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  // ------------------------------------------------------------ UI

  @override
  Widget build(BuildContext context) {
    final int used = _name.text.trim().runes.length;
    final bool over = used > AppConfig.kMaxUserNameLength;

    return Scaffold(
      backgroundColor: Colors.white,
      appBar: AppBar(
        backgroundColor: const Color(0xFFF7F7F7),
        elevation: 0.5,
        foregroundColor: Colors.black87,
        automaticallyImplyLeading: !widget.isFirstRun,
        title: Text(
          widget.isFirstRun ? '完善资料' : '个人资料',
          style: const TextStyle(fontSize: 16),
        ),
      ),
      body: ListView(
        padding: const EdgeInsets.fromLTRB(20, 24, 20, 32),
        children: <Widget>[
          // 用户 2026-09-27 要求：删掉「先设置你自己的昵称和头像 /
          // 之后一直沿用，随时可以点头像再改。」和「点头像从相册选择」两段提示文字，
          // 页面上只留头像 + 昵称 + 登录按钮，不要任何说明性废话。
          const SizedBox(height: 12),
          Center(child: _avatarPicker()),
          // 只在已经选好图、等用户点保存时给一句反馈，平时不显示任何提示。
          if (_pendingAvatar != null) ...<Widget>[
            const SizedBox(height: 10),
            Center(
              child: Text(
                '已选好新头像（$_pendingAvatarKb KB）',
                style: const TextStyle(fontSize: 12, color: Color(0xFF999999)),
              ),
            ),
          ],
          const SizedBox(height: 28),
          const Text('昵称',
              style: TextStyle(fontSize: 14, color: Color(0xFF333333))),
          const SizedBox(height: 8),
          TextField(
            controller: _name,
            autofocus: false,
            maxLines: 1,
            // 输入时就截断到 32 字符，用户看不到“超长但还能继续打字”的错觉。
            inputFormatters: <TextInputFormatter>[
              _UserNameLimiter(),
            ],
            onChanged: (_) => setState(() {}),
            onSubmitted: (_) => _save(),
            decoration: InputDecoration(
              hintText: '比如：阿伟',
              hintStyle: const TextStyle(fontSize: 13, color: Color(0xFFBBBBBB)),
              isDense: true,
              counterText: '',
              contentPadding:
                  const EdgeInsets.symmetric(horizontal: 12, vertical: 14),
              border: OutlineInputBorder(
                borderRadius: BorderRadius.circular(6),
                borderSide: const BorderSide(color: Color(0xFFDDDDDD)),
              ),
            ),
          ),
          const SizedBox(height: 6),
          Align(
            alignment: Alignment.centerRight,
            child: Text(
              '$used / ${AppConfig.kMaxUserNameLength}',
              style: TextStyle(
                fontSize: 12,
                color: over ? const Color(0xFFD93025) : const Color(0xFF999999),
              ),
            ),
          ),
          // 用户 2026-09-27 要求：
          // - 「开始使用」改叫「登录」；
          // - 删掉「昵称和头像只保存在这台手机上，不会上传到服务端。」这句；
          // - 按钮缩小，不要横着霸占整个屏幕（改成固定宽 132、高 40、居中）。
          const SizedBox(height: 22),
          Center(
            child: SizedBox(
              width: 132,
              height: 40,
              child: ElevatedButton(
                onPressed: _busy ? null : _save,
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
                child: const Text('保存'),
              ),
            ),
          ),

          // ---------------- 修改密码（第六轮要求） ----------------
          const SizedBox(height: 34),
          const Divider(),
          const SizedBox(height: 14),
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
          const SizedBox(height: 8),
          Center(
            child: Text(
              '账号：${_cfg.accountId.isEmpty ? '—' : _cfg.accountId}',
              style: const TextStyle(fontSize: 12, color: Color(0xFF999999)),
            ),
          ),
        ],
      ),
    );
  }

  /// 修改密码：弹框收旧密码 + 新密码（+ 确认），交给服务端验旧密码。
  ///
  /// 新旧密码规则都是「6~32 位、至少一个字母 + 一个数字」
  /// （服务端 accounts.validate_password，客户端同一套规则先拦一遍）。
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
                helperText: '6~32 位，至少一个字母和一个数字',
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

    // 对话框关掉后 controller 就该释放，值先取出来。
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

  /// 圆形头像：优先显示本次刚选的图（还没落盘），否则读已保存的配置。
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
      onTap: _busy ? null : _pickAvatar,
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

/// 昵称长度限制：按字符（rune）而不是 UTF-16 计数，emoji 不会被截成半个。
///
/// 做成 TextInputFormatter 而不是只在保存时校验，是为了让用户在输入阶段
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