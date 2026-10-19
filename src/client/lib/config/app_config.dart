// 配置模块：集中管理运行期设置项。
//
// 职责：管理**只该留在本机**的东西 —— 服务器地址、账号 ID、登录令牌，
// 以及一份「服务端账号资料」的本地缓存（离线时能看，联网后被服务端覆盖）。
//

import 'dart:io';

import 'package:flutter/foundation.dart';
// ImageProvider / FileImage / AssetImage 在 widgets.dart 里（foundation.dart 没有），
// 所以两个都要引：foundation 给 defaultTargetPlatform，widgets 给图片类。
import 'package:flutter/widgets.dart';

import '../services/local_store.dart';
import 'app_assets.dart';

class AppConfig extends ChangeNotifier {
  AppConfig._();

  static final AppConfig instance = AppConfig._();

  static const String _kServerUrl = 'server_url';
  // 2026-10-02 第二十九轮：云端地址（Cloudflare 隧道）。
  // 隧道网址每次重启都会变，没法写死在包里，只能由电脑通过局域网发现告诉手机，
  // 手机存下来，出门时 WsClient 自动切过去（见 ws_client.dart 的 _switchToCloudIfPossible）。
  static const String _kCloudUrl = 'cloud_url';
  static const String _kAppName = 'app_name';
  static const String _kAiName = 'ai_name';
  static const String _kAvatarId = 'avatar_id';
  static const String _kAvatarFile = 'avatar_file';
  static const String _kIconId = 'icon_id';
  static const String _kLauncherIndex = 'launcher_index';
  static const String _kUserName = 'user_name';
  static const String _kUserAvatarFile = 'user_avatar_file';

  // 第六轮新增：登录凭证（唯一真正必须留本地的两样之一）。
  static const String _kAccountId = 'account_id';
  static const String _kToken = 'token';

  /// 用户昵称的最大长度（用户 2026-09-27 明确要求：最长 32 个字符）。
  static const int kMaxUserNameLength = 32;

  /// 打包时可以塞一个默认服务器地址进来（给「固定公网地址」的用户用）：
  ///
  /// ```
  /// scripts\build_apk_ci.ps1 -Batch round24 -DefaultServer wss://xxx.ngrok-free.app/ws
  /// ```
  ///
  /// 这样手机装上就**直接是这个地址**，用户什么都不用填 —— 也就是用户
  /// 2026-10-01 第二十三轮要的「手机只装这一个软件、跟局域网一样用」。
  static const String _kBuildDefaultServer =
      String.fromEnvironment('JIXUN_DEFAULT_SERVER');

  /// 默认服务器地址按平台区分：
  /// - 打包时用 --dart-define 指定过 -> 用它（固定公网地址，比如 ngrok 的域名）
  /// - Android 模拟器访问宿主机：ws://10.0.2.2:8000/ws
  /// - Android 真机 / 其他平台：本机回环，装到真机后到设置页改成电脑局域网 IP
  static String get _defaultServerUrl {
    if (_kBuildDefaultServer.trim().isNotEmpty) {
      return normalizeServerUrl(_kBuildDefaultServer.trim());
    }
    if (defaultTargetPlatform == TargetPlatform.android) {
      return 'ws://10.0.2.2:8000/ws';
    }
    return 'ws://127.0.0.1:8000/ws';
  }

  String _serverUrl = _defaultServerUrl;

  /// 电脑通过局域网发现告诉我们的「云端地址」（Cloudflare 隧道，形如 wss://xxx/ws）。
  /// 空串 = 还不知道（没在家里连过、或电脑没开隧道）。
  String _cloudUrl = '';
  String _appName = kLauncherIdentities.first.label;
  // 2026-09-27 第五轮：全局设置页里的「AI 昵称 / AI 头像」版块已删除
  String _aiName = '';
  String _avatarId = kDefaultAvatarId;

  /// 从相册上传的自定义头像文件路径（PRD 3.1.2）。空串表示用内置头像。
  String _avatarFile = '';

  String _iconId = kDefaultIconId;
  int _launcherIndex = 1;

  /// 本机的设置文件里**有没有存过**桌面身份下标。
  ///
  bool _launcherIndexStored = false;

  /// 用户自己的昵称（顶栏左上角显示的那个）。空串表示还没设置过。
  String _userName = '';

  /// 用户自己从相册选的头像文件路径。
  ///
  /// 第六轮起这是**服务端头像的本地缓存**：登录后 Session 会把服务端
  /// 头像下载到这里，供 Image.file 离线显示；退出登录时清掉。
  String _userAvatarFile = '';

  /// 4 位账号 ID（`0000`~`9999`）。空串表示还没登录。
  String _accountId = '';

  String _token = '';

  String get serverUrl => _serverUrl;
  String get cloudUrl => _cloudUrl;
  String get accountId => _accountId;
  String get token => _token;

  /// 是否已经登录（有账号 ID 且有令牌）。
  bool get isLoggedIn => _accountId.isNotEmpty && _token.isNotEmpty;
  String get appName => _appName;
  String get aiName => _aiName;
  String get avatarId => _avatarId;
  String get avatarFile => _avatarFile;
  String get iconId => _iconId;
  int get launcherIndex => _launcherIndex;

  /// 本机是否存过桌面身份（存过就以本机为准，见 [_launcherIndexStored]）。
  bool get launcherIndexStored => _launcherIndexStored;
  String get userName => _userName;
  String get userAvatarFile => _userAvatarFile;

  /// 用户是否已经设置过自己的头像（文件确实存在才算）。
  bool get hasUserAvatar =>
      _userAvatarFile.isNotEmpty && File(_userAvatarFile).existsSync();

  /// 首次安装是否需要先填资料（昵称或头像任一没设过就要填）。
  ///
  /// 用户 2026-09-27 要求：“用户第 1 次安装会被要求填写昵称和设置头像，
  /// 之后就一直沿用这个昵称和头像”。
  ///
  /// 头像这一项只在**有相册可选的平台**（Android，走原生通道 jixun/picker）
  /// 才强制。Windows 桌面端跑 Flutter 时选图会返回 unsupported，
  /// 若也强制要求头像，用户会在引导页上永远出不去 —— 那是死锁，不是需求。
  /// 第六轮起这个方法**不再决定首屏去哪** —— 首屏改成看 isLoggedIn：
  /// 没登录就去注册/登录页，登录了才进首页。这里保留是为了兼容旧调用点，
  /// 语义变成「资料是否完整」（头像 + 昵称都有）。
  bool get needsProfileSetup {
    if (_userName.trim().isEmpty) return true;
    if (galleryPickerAvailable && !hasUserAvatar) return true;
    return false;
  }

  /// 当前平台是否能打开系统相册选图。
  ///
  /// 与 ImagePickerService 的判断保持一致（那里是 kIsWeb || 非 Android 即 unsupported）。
  /// 这里只做平台判断，不引入对 services 层的依赖，避免配置层反向依赖服务层。
  static bool get galleryPickerAvailable =>
      !kIsWeb && defaultTargetPlatform == TargetPlatform.android;

  /// 用户头像控件用；**没设置过时返回 null**，由调用方画一个中性的人形占位。
  ///
  /// 2026-09-27 修正（用户指出“你把用户强制性变成了那个很丑的头像”）：
  /// 这里原来在没设置时回退到内置头像（kAvatarAssets.last），
  /// 等于替用户挑了一张他根本没选过的脸，看起来就像被强行换头。
  /// 用户头像必须是用户自己从相册选的；没选过就不显示任何头像，
  /// 只画一个灰色人形轮廓，绝不再拿内置头像顶替。
  ImageProvider? get userAvatarImage =>
      hasUserAvatar ? FileImage(File(_userAvatarFile)) : null;

  /// 用户头像对应的本地文件（没有则 null）。
  File? get userAvatarFileOrNull => hasUserAvatar ? File(_userAvatarFile) : null;

  /// 当前账号的密码规则（与服务端 accounts.validate_password 保持一致）。
  ///
  static const int kPasswordMinLength = 6;
  static const int kPasswordMaxLength = 32;

  /// 返回 null 表示可用，否则返回给用户看的错误文案。
  static String? validatePassword(String raw) {
    if (raw.length < kPasswordMinLength) {
      return '密码至少 $kPasswordMinLength 位';
    }
    if (raw.length > kPasswordMaxLength) {
      return '密码最长 $kPasswordMaxLength 位';
    }
    if (!RegExp(r'[A-Za-z]').hasMatch(raw)) return '密码至少要有一个字母';
    if (!RegExp(r'[0-9]').hasMatch(raw)) return '密码至少要有一个数字';
    return null;
  }

  /// 账号 ID 校验：4 位数字，允许用户少打前导零（服务端会补齐）。
  static String? validateAccountId(String raw) {
    final String s = raw.trim();
    if (s.isEmpty) return '账号不能为空';
    if (!RegExp(r'^[0-9]{1,4}$').hasMatch(s)) return '账号是 4 位数字，例如 0001';
    return null;
  }

  /// 昵称清洗：去掉首尾空白、把换行/制表符换成空格、超长按字符截断到 32。
  ///
  static String sanitizeUserName(String raw) {
    final String s = raw.replaceAll(RegExp(r'[\r\n\t]'), ' ').trim();
    final List<int> runes = s.runes.toList();
    if (runes.length <= kMaxUserNameLength) return s;
    return String.fromCharCodes(runes.take(kMaxUserNameLength));
  }

  /// 昵称校验：返回 null 表示可用，否则返回给用户看的错误文案。
  static String? validateUserName(String raw) {
    final String s = raw.trim();
    if (s.isEmpty) return '昵称不能为空';
    if (s.runes.length > kMaxUserNameLength) {
      return '昵称最长 $kMaxUserNameLength 个字符';
    }
    return null;
  }

  /// 是否正在使用从相册上传的自定义头像（文件确实存在才算）。
  bool get hasCustomAvatar =>
      _avatarFile.isNotEmpty && File(_avatarFile).existsSync();

  /// AI 头像的本地资源路径（永远不为 null，找不到就回退默认头像）。
  ///
  /// 内置头像走 assets（只读资源），自定义头像走本地文件（PRD 3.1.2）。
  /// 两者都不联网。
  String get avatarAsset =>
      avatarAssetById(_avatarId) ?? kAvatarAssets.first.asset;

  /// 头像控件用：自定义头像用 FileImage，内置头像用 AssetImage。
  ImageProvider get avatarImage {
    if (hasCustomAvatar) return FileImage(File(_avatarFile));
    return AssetImage(avatarAsset);
  }

  /// 软件图标的本地资源路径（App 内部展示用）。
  String get iconAsset =>
      iconAssetById(_iconId) ?? kIconAssets.first.asset;

  Future<void> load() async {
    final Map<String, dynamic> data = await LocalStore.read();
    _serverUrl = data[_kServerUrl] as String? ?? _serverUrl;
    _cloudUrl = data[_kCloudUrl] as String? ?? _cloudUrl;
    _appName = data[_kAppName] as String? ?? _appName;
    _aiName = data[_kAiName] as String? ?? _aiName;
    _avatarId = data[_kAvatarId] as String? ?? _avatarId;
    _avatarFile = data[_kAvatarFile] as String? ?? _avatarFile;
    _iconId = data[_kIconId] as String? ?? _iconId;
    _launcherIndex = (data[_kLauncherIndex] as num?)?.toInt() ?? _launcherIndex;
    _launcherIndexStored = data.containsKey(_kLauncherIndex);
    _userName = data[_kUserName] as String? ?? _userName;
    _userAvatarFile = data[_kUserAvatarFile] as String? ?? _userAvatarFile;
    _accountId = data[_kAccountId] as String? ?? _accountId;
    _token = data[_kToken] as String? ?? _token;
    notifyListeners();
  }

  /// 登录成功后写入凭证（由 Session 调用）。
  Future<void> saveCredentials({
    required String accountId,
    required String token,
  }) async {
    _accountId = accountId;
    _token = token;
    await _persist();
    notifyListeners();
  }

  /// 清掉登录凭证与全部服务端资料缓存（退出登录 / 被踢下线时调用）。
  ///
  /// 刻意**保留 server_url** —— 退出登录后还要知道连哪台服务器，
  /// 否则用户得重新输一遍 IP 才能登录，那是没必要的折磨。
  Future<void> clearSession() async {
    _accountId = '';
    _token = '';
    _userName = '';
    _userAvatarFile = '';
    _appName = kLauncherIdentities.first.label;
    _iconId = kDefaultIconId;
    _launcherIndex = 1;
    await _persist();
    notifyListeners();
  }

  /// 规范化用户填的服务器地址。
  ///
  /// 常见输入问题：带空格、少了 ws:// 前缀、末尾多了 /、填了 http://。
  /// 这里统一修好，避免“地址看着对但连不上”。
  static String normalizeServerUrl(String raw) {
    String s = raw.trim();
    if (s.isEmpty) return s;
    if (s.startsWith('http://')) {
      s = 'ws://${s.substring('http://'.length)}';
    } else if (s.startsWith('https://')) {
      s = 'wss://${s.substring('https://'.length)}';
    } else if (!s.startsWith('ws://') && !s.startsWith('wss://')) {
      s = 'ws://$s';
    }
    // 2026-10-02 第二十九轮：IPv6 地址必须带方括号，否则一定连不上。
    //
    s = _bracketIpv6Host(s);

    // 只填了 host:port（或末尾多个斜杠）时补上协议路径 /ws。
    //
    // 2026-09-27 修 bug：原来是 `s = '$s/ws'` 直接拼。用户填
    // `https://example.com:8443/` 时，uri.path 正好是 '/'，拼出来就是
    // `wss://example.com:8443//ws` —— 双斜杠，服务端路由匹配不上，
    // 表现是「地址看着对却永远连不上」。所以先把尾部斜杠剪干净再拼。
    final Uri? uri = Uri.tryParse(s);
    if (uri != null && (uri.path.isEmpty || uri.path == '/')) {
      final String trimmed = s.replaceFirst(RegExp(r'/+$'), '');
      s = '$trimmed/ws';
    }

    // 2026-10-02 第二十九轮（用户实测报错：「输入这个想要连接说后面要带端口，8000」）：
    // Dart 的 Uri **只认得 http/https 的默认端口，不认 ws/wss** ——
    // 实测 `Uri.parse('wss://x.trycloudflare.com/ws').port` 返回 **0**。
    // 于是：
    //   · 校验会误报「地址里要带端口」；
    //   · 后面拼 WebSocket 地址时也拿不到正确端口（wss 应该是 443）。
    // 这里直接按协议把默认端口补成**显式**的，后面所有地方都不会再算错：
    //   wss://host/ws  -> wss://host:443/ws
    //   ws://host/ws   -> ws://host:8000/ws（本项目的服务端端口）
    s = _fillDefaultPort(s);
    return s;
  }

  /// 按协议补上默认端口（只补「确实没写端口」的，已写端口的一律不动）。
  ///
  /// 为什么要自己补：Dart 的 `Uri` 只给 http/https 定义默认端口，ws/wss 会解析成 0；
  /// 而 Cloudflare 隧道给的地址形如 `wss://xxx.trycloudflare.com/ws`（天然不带端口），
  /// 必须要走 443 才能连上。
  static String _fillDefaultPort(String s) {
    final int schemeEnd = s.indexOf('://');
    if (schemeEnd < 0) return s;
    final String scheme = s.substring(0, schemeEnd).toLowerCase();
    final String rest = s.substring(schemeEnd + 3);

    // 拆出 authority 与 path/query
    int cut = rest.length;
    for (final String mark in <String>['/', '?', '#']) {
      final int i = rest.indexOf(mark);
      if (i >= 0 && i < cut) cut = i;
    }
    final String authority = rest.substring(0, cut);
    final String tail = rest.substring(cut);
    if (authority.isEmpty) return s;

    // 已经写了端口就不动（IPv6 是 [..]:port 形式，方括号里的冒号不算）
    final bool hasPort = authority.startsWith('[')
        ? authority.contains(']:')
        : authority.contains(':');
    if (hasPort) return s;

    final int? port = switch (scheme) {
      'wss' || 'https' => 443,
      'ws' || 'http' => 8000, // 本项目的服务端固定 8000
      _ => null,
    };
    if (port == null) return s;
    return '$scheme://$authority:$port$tail';
  }

  /// 把「裸 IPv6」主机名自动加上方括号（[ ... ]）。
  ///
  /// 支持的输入（协议前缀已经在这一步之前补好）：
  ///   ws://2409:896a:393b:bef::53fd/ws        -> ws://[2409:896a:393b:bef::53fd]/ws
  ///   ws://2409:896a:393b:bef::53fd:8000/ws   -> ws://[2409:896a:393b:bef::53fd]:8000/ws
  /// 已经带方括号的、普通 IPv4/域名、user@host 形式一律原样返回（不乱改）。
  static String _bracketIpv6Host(String s) {
    final int schemeEnd = s.indexOf('://');
    if (schemeEnd < 0) return s;
    final String head = s.substring(0, schemeEnd + 3);
    final String rest = s.substring(schemeEnd + 3);

    // 拆出 authority（到第一个 / ? # 为止）和后面的路径/查询
    int cut = rest.length;
    for (final String mark in <String>['/', '?', '#']) {
      final int i = rest.indexOf(mark);
      if (i >= 0 && i < cut) cut = i;
    }
    final String authority = rest.substring(0, cut);
    final String tail = rest.substring(cut);

    if (authority.isEmpty) return s;
    if (authority.contains('[')) return s; // 已经括好了
    if (authority.contains('@')) return s; // 带账号密码的形式不碰
    // 至少两个冒号才可能是 IPv6（host:port 只有一个）
    if (':'.allMatches(authority).length < 2) return s;

    String host = authority;
    String portPart = '';
    // 末尾是 1~5 位纯数字时，可能是端口 —— 只有当去掉它之后仍是合法 IPv6 才认
    final int lastColon = authority.lastIndexOf(':');
    final String maybePort = authority.substring(lastColon + 1);
    if (RegExp(r'^\d{1,5}$').hasMatch(maybePort)) {
      final String maybeHost = authority.substring(0, lastColon);
      if (InternetAddress.tryParse(maybeHost) is InternetAddress) {
        host = maybeHost;
        portPart = ':$maybePort';
      }
    }
    // 必须真是 IPv6 才动它（域名里带冒号是不可能的，但保守一点没坏处）
    final InternetAddress? parsed = InternetAddress.tryParse(host);
    if (parsed == null || parsed.type != InternetAddressType.IPv6) return s;
    return '$head[$host]$portPart$tail';
  }

  /// 地址是否看起来可用（只做静态校验，不做联网探测）。
  ///
  static String? validateServerUrl(String raw) {
    final String s = normalizeServerUrl(raw);
    if (s.isEmpty) return '服务器地址不能为空';
    final Uri? uri = Uri.tryParse(s);
    if (uri == null || uri.host.isEmpty) return '地址格式不对，例如 ws://192.168.x.x:8000/ws';
    return null;
  }

  Future<void> update({
    String? serverUrl,
    String? cloudUrl,
    String? appName,
    String? aiName,
    String? avatarId,
    String? avatarFile,
    bool clearAvatarFile = false,
    String? iconId,
    int? launcherIndex,
    String? userName,
    String? userAvatarFile,
    bool clearUserAvatarFile = false,
  }) async {
    if (serverUrl != null && serverUrl.trim().isNotEmpty) {
      _serverUrl = normalizeServerUrl(serverUrl);
    }
    if (cloudUrl != null) {
      // 允许传空串把「已知的云端地址」清掉（比如隧道关了、换了新地址）
      _cloudUrl = cloudUrl.trim();
    }
    if (appName != null && appName.trim().isNotEmpty) {
      _appName = appName.trim();
    }
    if (aiName != null && aiName.trim().isNotEmpty) {
      _aiName = aiName.trim();
    }
    if (avatarId != null && avatarId.trim().isNotEmpty) {
      _avatarId = avatarId.trim();
      // 选了内置头像就放弃自定义头像，避免“选了没反应”
      _avatarFile = '';
    }
    if (avatarFile != null && avatarFile.trim().isNotEmpty) {
      _avatarFile = avatarFile.trim();
    }
    if (clearAvatarFile) {
      _avatarFile = '';
    }
    if (iconId != null && iconId.trim().isNotEmpty) {
      _iconId = iconId.trim();
    }
    if (launcherIndex != null) {
      _launcherIndex = launcherIndex;
    }
    if (userName != null) {
      _userName = sanitizeUserName(userName);
    }
    if (userAvatarFile != null && userAvatarFile.trim().isNotEmpty) {
      _userAvatarFile = userAvatarFile.trim();
    }
    if (clearUserAvatarFile) {
      _userAvatarFile = '';
    }
    await _persist();
    notifyListeners();
  }

  /// 统一的落盘入口：所有字段一次写全，避免遗漏。
  ///
  /// 2026-10-01 第二十一轮：改成 [LocalStore.merge]（只覆盖自己这几个键）。
  /// 原来是整份 `write`，会把同一个文件里**别人**的键抹掉 ——
  /// 桌面身份的「已复位」标记和消息 resume 锚点就是这么反复丢的（详见
  /// LocalStore.merge 的注释），也正是用户看到的「切完微信、进去又变回企鹅」
  /// 和「一进来 90 多条历史」的真正原因。
  Future<void> _persist() async {
    await LocalStore.merge(<String, dynamic>{
      _kServerUrl: _serverUrl,
      _kCloudUrl: _cloudUrl,
      _kAppName: _appName,
      _kAiName: _aiName,
      _kAvatarId: _avatarId,
      _kAvatarFile: _avatarFile,
      _kIconId: _iconId,
      _kLauncherIndex: _launcherIndex,
      _kUserName: _userName,
      _kUserAvatarFile: _userAvatarFile,
      _kAccountId: _accountId,
      _kToken: _token,
    });
  }
}
