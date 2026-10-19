// 登录态服务（用户 2026-09-27 第六轮要求）。
//

import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/foundation.dart';

import '../config/app_config.dart';
import 'api_client.dart';
import 'avatar_store.dart';
import 'local_queue.dart';
import 'local_store.dart';
import 'ws_client.dart';

class Session extends ChangeNotifier {
  Session._();

  static final Session instance = Session._();

  /// 正在登录/注册（按钮转圈并禁用）。
  bool _busy = false;
  bool get busy => _busy;

  int _cooldownSeconds = 0;
  int get cooldownSeconds => _cooldownSeconds;
  Timer? _cooldownTimer;

  bool get isLoggedIn => AppConfig.instance.isLoggedIn;

  /// 把 401 接到「清凭证」上。main.dart 在启动时调用一次。
  static void wireUnauthorizedHandler() {
    ApiClient.onUnauthorized = () {
      unawaited(Session.instance.handleUnauthorized());
    };
  }

  /// 令牌失效：断开 WS、清本地资料、回到登录页。
  Future<void> handleUnauthorized() async {
    if (!AppConfig.instance.isLoggedIn) return;
    WsClient.instance.disconnect();
    await AppConfig.instance.clearSession();
    _clearLocalCache();
    notifyListeners();
  }

  /// 主动退出登录。
  Future<void> logout() async {
    await ApiClient.instance.logout();
    WsClient.instance.disconnect();
    await AppConfig.instance.clearSession();
    _clearLocalCache();
    notifyListeners();
  }

  void _clearLocalCache() {
    // 服务端资料的本地缓存要清掉，下次登录换成新账号的资料。
    // 只清配置与头像文件，不动聊天记录缓存目录（那是按账号分开的）。
    unawaited(AvatarStore.clearUser());
    // 2026-09-28 第八轮：会话 / 消息 / 去重集合 / resume 锚点也必须一起清。
    // 用户实测的「凭空多出一个小张」就是这里漏了 —— 从 0001 退出登进 0000，
    // 上一个账号的会话还挂在列表里，点进去服务端 404。
    LocalQueue.instance.clearAll();
  }

  // ------------------------------------------------------------ 冷却

  void startCooldown(int seconds) {
    _cooldownTimer?.cancel();
    _cooldownSeconds = seconds;
    notifyListeners();
    _cooldownTimer = Timer.periodic(const Duration(seconds: 1), (Timer t) {
      _cooldownSeconds -= 1;
      if (_cooldownSeconds <= 0) {
        _cooldownSeconds = 0;
        t.cancel();
      }
      notifyListeners();
    });
  }

  // ------------------------------------------------------------ 注册 / 登录

  /// 注册。成功返回 null，失败返回给用户看的错误文案。
  ///
  /// [avatarBytes] 是刚从相册选的图（还没落盘）；[avatarExt] 给服务端决定后缀。
  Future<String?> register({
    required String nickname,
    required String password,
    Uint8List? avatarBytes,
    String avatarExt = 'jpg',
  }) async {
    final String? nameErr = AppConfig.validateUserName(nickname);
    if (nameErr != null) return nameErr;
    final String? pwdErr = AppConfig.validatePassword(password);
    if (pwdErr != null) return pwdErr;
    if (avatarBytes == null) return '请先从相册选一张头像';

    _busy = true;
    notifyListeners();
    try {
      final Map<String, dynamic> res = await ApiClient.instance.register(
        nickname: AppConfig.sanitizeUserName(nickname),
        password: password,
        avatar: base64Encode(avatarBytes),
        avatarExt: avatarExt,
      );
      await _adoptLogin(res, avatarBytes: avatarBytes);
      return null;
    } on ApiException catch (e) {
      return e.message;
    } finally {
      _busy = false;
      notifyListeners();
    }
  }

  /// 登录。成功返回 null，失败返回给用户看的错误文案。
  Future<String?> login({
    required String accountId,
    required String password,
  }) async {
    final String? idErr = AppConfig.validateAccountId(accountId);
    if (idErr != null) return idErr;
    if (password.isEmpty) return '请输入密码';

    _busy = true;
    notifyListeners();
    try {
      final Map<String, dynamic> res = await ApiClient.instance.login(
        accountId: accountId.trim(),
        password: password,
      );
      await _adoptLogin(res);
      return null;
    } on ApiException catch (e) {
      // 429 = 密码刚错过，还在 3 秒冷却里。
      if (e.statusCode == 429) {
        startCooldown(3);
        return '密码错误，请 ${_cooldownSeconds + 1} 秒后再试';
      }
      return e.message;
    } finally {
      _busy = false;
      notifyListeners();
    }
  }

  /// 登录/注册成功后的共同收尾：存凭证 -> 拉资料 -> 连 WS。
  Future<void> _adoptLogin(
    Map<String, dynamic> res, {
    Uint8List? avatarBytes,
  }) async {
    final String accountId = res['account_id']?.toString() ?? '';
    final String token = res['token']?.toString() ?? '';
    if (accountId.isEmpty || token.isEmpty) {
      throw ApiException('服务端没有返回账号或令牌');
    }
    await AppConfig.instance.saveCredentials(accountId: accountId, token: token);

    // 头像是刚选的图，直接落本地，省一次下载。
    if (avatarBytes != null) {
      final String? path = await AvatarStore.saveUser(avatarBytes);
      if (path != null) {
        await AppConfig.instance.update(userAvatarFile: path);
      }
    }
    // 拉一次服务端资料，把昵称/头像/自定义项同步到本地。
    await sync();
    // 凭证就绪后再连 WS（连接地址上要带 token）。
    WsClient.instance.connect(force: true);
  }

  /// 从服务端拉取账号资料与好友列表，写入本地缓存。
  ///
  Future<void> sync() async {
    if (!isLoggedIn) return;
    try {
      final Map<String, dynamic> data = await ApiClient.instance.sync();
      final dynamic account = data['account'];
      if (account is! Map) return;
      final String nickname = account['nickname']?.toString() ?? '';
      final String appName = account['app_name']?.toString() ?? '';
      final String iconId = account['icon_id']?.toString() ?? '';
      final int launcher = (account['launcher_index'] as num?)?.toInt() ?? 1;
      final String avatarUrl = account['avatar_url']?.toString() ?? '';

      await AppConfig.instance.update(
        userName: nickname.isEmpty ? null : nickname,
        appName: appName.isEmpty ? null : appName,
        iconId: iconId.isEmpty ? null : iconId,
        // 2026-10-01 第十八轮 b：本机已经选过桌面身份就以本机为准，
        // 服务端那份只在「新手机 / 没选过」时用来恢复 ——
        launcherIndex: AppConfig.instance.launcherIndexStored ? null : launcher,
      );
      await _cacheAvatar(avatarUrl);
      notifyListeners();
    } on ApiException {
      // 拉不到就先用本地缓存顶着（断网时 App 还能开）。
    }
  }

  /// 把服务端头像下载到本地缓存文件，供 Image.file 离线显示。
  Future<void> _cacheAvatar(String avatarUrl) async {
    if (avatarUrl.isEmpty) return;
    try {
      final String base = ApiClient.httpBase();
      if (base.isEmpty) return;
      final HttpClient client = HttpClient()
        ..connectionTimeout = const Duration(seconds: 8);
      try {
        final HttpClientRequest req =
            await client.getUrl(Uri.parse('$base$avatarUrl'));
        final HttpClientResponse res = await req.close();
        if (res.statusCode != 200) return;
        final List<int> bytes = <int>[];
        await for (final List<int> chunk in res) {
          bytes.addAll(chunk);
        }
        if (bytes.isEmpty) return;
        final String? path = await AvatarStore.saveUser(Uint8List.fromList(bytes));
        if (path != null) {
          await AppConfig.instance.update(userAvatarFile: path);
        }
      } finally {
        client.close(force: true);
      }
    } catch (_) {
      // 头像缓存失败不影响登录：先用占位图显示。
    }
  }

  /// 本地缓存目录（留作扩展：按账号分目录缓存聊天记录）。
  Future<Directory> cacheDir() async => LocalStore.dir();
}
