// 服务端头像的本地缓存（用户 2026-09-27 第八轮要求：「头像昵称实时同步」）。
//
// 问题（我改之前代码里的真实状态）：
//
// 服务端 `_conversation_to_json` 把 `ai_avatar` / `peer_avatar` 给出来，
// 值是 `ai_profiles.avatar_url` / `users.avatar_url` —— 形如
// `/uploads/avatars/0001.jpg` 的**服务端 HTTP 路径**（见 main._save_avatar_base64）。
//
// 而客户端 `home_page._avatar()` 与 `bubble._AiAvatar()` 拿到这个字符串就直接
// `File(path).existsSync()` —— 手机上当然不存在这个路径，于是**永远画中性人形**。
// 表现出来就是「给好友换了头像，列表和气泡都不动」。
//
// 修法：把服务端路径下载成本地文件，之后走 `Image.file`（离线也能看、不闪）。
// 文件名用 URL 的哈希 + 路径里的原始文件名，这样：
//   · 同一个 URL 只下一次；
//   · 服务端换了头像（URL 变了）自然就是新文件，不需要手动失效；
//   · 哈希前缀避免不同目录下的同名文件互相覆盖。
//
// 不引入 path_provider / cached_network_image：沿用 local_store.dart 已经验证过的
// 目录管理使用 dart:io：原生插件需要 Windows 开发者模式，多数环境无法开启。

import 'dart:async';
import 'dart:io';

import 'package:flutter/foundation.dart';

import 'api_client.dart';
import 'local_store.dart';

class AvatarCache extends ChangeNotifier {
  AvatarCache._();

  static final AvatarCache instance = AvatarCache._();

  /// url -> 本地文件路径。已经下好的在这里，避免每次 build 都碰磁盘。
  final Map<String, String> _local = <String, String>{};

  /// 正在下载的 url，防止同一张图并发下十几次（列表里每个气泡都会调 ensure）。
  final Set<String> _inflight = <String>{};

  /// 已经尝试过但失败的 url：不再无限重试，否则断网时会疯狂发请求。
  final Set<String> _failed = <String>{};

  /// 每个 url 现在是第几代，每次 [invalidate] 加一。
  ///
  /// 用来解决「下载途中用户又换了一次头像」：晚到的旧字节不能把新头像盖回去。
  final Map<String, int> _generation = <String, int>{};

  /// 这个字符串值不值得去服务器请求。
  ///
  /// 2026-09-28 第九轮新增（用户日志里那条
  /// `GET /data/user/0/com.jixun.jixun_client/code_cache/.../ai_avatar.jpg
  /// 404 Not Found` 的最后一道防线）。
  ///
  /// 历史库里存过**手机本地路径**：服务端上当然没有这个文件，而我们又拿它
  /// 拼上服务器地址去请求，于是每次渲染头像都往服务端打一条 404。
  /// 服务端已经不再接受这种值（main._is_usable_avatar_url），但旧数据还在，
  /// 而且客户端不该把「本机路径」当「服务端资源」—— 两头都拦一次最稳。
  static bool _looksLikeServerResource(String url) {
    return url.startsWith('/uploads/') ||
        url.startsWith('http://') ||
        url.startsWith('https://');
  }

  /// 同步取本地文件路径；没有（或还没下好）返回 null。
  ///
  /// **不阻塞**：widget 每次 build 都能调，没下好就先画占位，下好之后
  /// 通过 notifyListeners 触发重建。
  String? localPath(String? url) {
    if (url == null || url.isEmpty) return null;
    // 本机路径 / 服务端路径混用过的历史数据：只处理服务端资源，其余按“没有头像”。
    if (!_looksLikeServerResource(url)) {
      final File local = File(url);
      if (local.existsSync()) return url;
      return null;
    }
    final String? cached = _local[url];
    if (cached != null) {
      return File(cached).existsSync() ? cached : null;
    }
    // 首次见到这个 url：登记下载，本次仍返回 null。
    unawaited(ensure(url));
    return null;
  }

  /// 确保这个 url 已下载到本地（幂等、可重复调用）。
  Future<void> ensure(String? url) async {
    if (url == null || url.isEmpty) return;
    if (!_looksLikeServerResource(url)) return;
    if (_local.containsKey(url) || _inflight.contains(url) || _failed.contains(url)) {
      return;
    }
    _inflight.add(url);
    final int generation = _generation[url] ?? 0;
    try {
      final String? path = await _download(url, generation);
      if (path != null) {
        _local[url] = path;
        notifyListeners();
      } else {
        _failed.add(url);
      }
    } finally {
      _inflight.remove(url);
    }
  }

  /// 让某个 url 的本地缓存失效：内存映射、失败标记、本地文件一起清掉，
  /// 下次渲染头像时会重新下载。
  ///
  /// 2026-09-30 第十五轮（用户报告「给模型更换头像之后聊天界面不会实时更新」）：
  /// 服务端换头像时是**覆盖同一个文件名**（`/uploads/avatars/ai7.jpg`），
  /// URL 一个字都没变，而这里的缓存是按 URL 记的 —— 命中就直接返回上一张脸，
  /// 连磁盘上那个旧文件都不会重新下（[_download] 里 `target.existsSync()`
  /// 会把它直接返回）。所以「刚改过头像」的地方必须显式调一次它。
  Future<void> invalidate(String? url) async {
    if (url == null || url.isEmpty) return;
    if (!_looksLikeServerResource(url)) return;
    // 代次 +1：已经在路上的那次下载回来时会被丢弃，不会把新头像盖成旧的。
    _generation[url] = (_generation[url] ?? 0) + 1;
    _local.remove(url);
    _failed.remove(url);
    _inflight.remove(url);
    try {
      final File f = File(_pathFor(url));
      if (await f.exists()) await f.delete();
    } catch (_) {
      // 删不掉也不影响：内存映射已清，下一次下载会覆盖它。
    }
    notifyListeners();
  }

  /// 某个 url 对应的本地文件路径（下载与失效共用，避免两处各算一遍算错）。
  String _pathFor(String url) {
    final Directory dir = Directory(
      '${LocalStore.dir().path}${Platform.pathSeparator}avatars',
    );
    return '${dir.path}${Platform.pathSeparator}${_fileNameFor(url)}';
  }

  /// 服务端换过一批头像之后重新拉（比如从设置页回来）。目前只清失败标记，
  /// 已下好的按 URL 自然失效 —— URL 变了就是新的一条记录。
  void retryFailed() {
    if (_failed.isEmpty) return;
    _failed.clear();
    notifyListeners();
  }

  Future<String?> _download(String url, [int generation = 0]) async {
    // 服务端给的可能是 `/uploads/...`（相对），也可能是完整 http 地址。
    if (!_looksLikeServerResource(url)) return null;
    final String base = ApiClient.httpBase();
    final String full = url.startsWith('http') ? url : '$base$url';
    if (full.isEmpty || !full.startsWith('http')) return null;

    final Directory dir = Directory(
      '${LocalStore.dir().path}${Platform.pathSeparator}avatars',
    );
    try {
      if (!await dir.exists()) await dir.create(recursive: true);
    } catch (_) {
      return null;
    }
    final File target = File(_pathFor(url));
    if (target.existsSync()) return target.path;

    final HttpClient client = HttpClient()
      ..connectionTimeout = const Duration(seconds: 8);
    try {
      final HttpClientRequest req = await client.getUrl(Uri.parse(full));
      final HttpClientResponse res = await req.close();
      if (res.statusCode != 200) return null;
      final List<int> bytes = <int>[];
      await for (final List<int> chunk in res) {
        bytes.addAll(chunk);
      }
      if (bytes.isEmpty) return null;
      // 下载途中这个 url 被 invalidate 过（用户又换了一次头像）：
      // 手上这批字节已经过期，写下去会把新头像盖回旧的，直接丢弃。
      if ((_generation[url] ?? 0) != generation) return null;
      await target.writeAsBytes(Uint8List.fromList(bytes), flush: true);
      return target.path;
    } catch (_) {
      return null;
    } finally {
      client.close(force: true);
    }
  }

  /// 本地文件名 = 8 位哈希前缀 + 原文件名（保留后缀，Image.file 靠后缀解码）。
  static String _fileNameFor(String url) {
    int hash = 0;
    for (final int unit in url.codeUnits) {
      hash = (hash * 31 + unit) & 0x7fffffff;
    }
    final String tail = url.split('/').last.split('?').first;
    final String safe = tail.replaceAll(RegExp(r'[^A-Za-z0-9._-]'), '_');
    return '${hash.toRadixString(16)}_${safe.isEmpty ? 'avatar.jpg' : safe}';
  }

  /// 退出登录时清掉（换账号后不能看到上一个账号好友的脸）。
  Future<void> clear() async {
    _local.clear();
    _inflight.clear();
    _failed.clear();
    try {
      final Directory dir = Directory(
        '${LocalStore.dir().path}${Platform.pathSeparator}avatars',
      );
      if (await dir.exists()) await dir.delete(recursive: true);
    } catch (_) {
      // 删不掉不影响使用：内存映射已清，下次会重新下。
    }
    notifyListeners();
  }
}