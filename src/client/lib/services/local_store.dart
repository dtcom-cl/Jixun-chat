// 本地设置存储：设置项的持久化层。
//
// 依据：设置模块“依赖：本地存储”。
//
// 实现选择：用 dart:io 直接读写一个 settings.json，不引入 shared_preferences。
// 原因：shared_preferences 是原生插件，而 Windows 上构建含插件的 Flutter 工程
// 需要系统开启“开发者模式”（符号链接权限）；本机未开启且无法自动开启。
// 去掉插件后 Windows / Android 构建均不再需要开发者模式，依赖也更少
// 。
//

import 'dart:convert';
import 'dart:io';

import 'package:flutter/services.dart';

class LocalStore {
  LocalStore._();

  static const String _folderName = 'jixun_client';
  static const String _fileName = 'settings.json';

  /// Android 上真正的应用私有目录（filesDir 下面的子目录）。
  /// [init] 里取一次并缓存；为空表示没取到，此时退回老行为。
  static String? _androidDir;

  /// 最后一次写失败的原因（排障用：写不进去时必须能看出来，
  /// 否则用户只会看到「又要重新登录」）。
  static String? lastWriteError;

  static const MethodChannel _storage = MethodChannel('jixun/storage');

  /// 启动时调一次（main.dart 里、读配置之前）。非 Android 平台什么都不做。
  static Future<void> init() async {
    if (!Platform.isAndroid) return;
    try {
      final String? base = await _storage.invokeMethod<String>('filesDir');
      if (base == null || base.isEmpty) return;
      _androidDir = '$base${Platform.pathSeparator}$_folderName';
    } catch (_) {
      // 通道取不到（比如平台侧还没实现）就保持老行为，不让 App 起不来。
      return;
    }
    await _migrateFromLegacyDir();
  }

  /// 本应用的私有目录（设置文件、用户从相册选的 AI 头像都放这里）。
  ///
  /// PRD 3.1.2 要求“上传朋友的自拍作为 AI 头像”，图片必须落在本地文件里，
  /// 不能联网、也不能只存在内存中（重启就丢）。
  static Directory dir() => _dir();

  static Directory _dir() {
    if (Platform.isWindows) {
      final appData = Platform.environment['APPDATA'];
      if (appData != null && appData.isNotEmpty) {
        return Directory('$appData\\$_folderName');
      }
    }
    if (Platform.isLinux || Platform.isMacOS) {
      final home = Platform.environment['HOME'];
      if (home != null && home.isNotEmpty) {
        return Directory('$home/.$_folderName');
      }
    }
    // Android：优先用 init() 取到的私有目录（不会被系统当缓存清掉）。
    final String? androidDir = _androidDir;
    if (Platform.isAndroid && androidDir != null && androidDir.isNotEmpty) {
      return Directory(androidDir);
    }
    // 其它情况（含 iOS / 没取到通道）：沿用进程可写目录。
    return Directory('${Directory.systemTemp.path}${Platform.pathSeparator}$_folderName');
  }

  /// 老版本用的目录（Android 上是缓存目录下面的那个）。
  static Directory legacyDir() =>
      Directory('${Directory.systemTemp.path}${Platform.pathSeparator}$_folderName');

  /// 把老目录里的东西搬到新目录（只在**新目录还没有配置**时做一次）。
  ///
  /// 返回搬过去的文件数。搬不过去不影响使用（大不了重新登录一次），
  /// 所以这里所有异常都吞掉。
  static Future<void> _migrateFromLegacyDir() async {
    try {
      final Directory old = legacyDir();
      final Directory target = _dir();
      if (old.path == target.path) return;
      if (!await old.exists()) return;
      // 新目录已经有配置了就别覆盖（否则每次启动都会把新数据盖回旧的）。
      if (await File('${target.path}${Platform.pathSeparator}$_fileName').exists()) {
        return;
      }
      await migrate(old, target);
    } catch (_) {
      // 搬家失败不影响使用。
    }
  }

  /// 递归复制目录内容，返回复制的文件数（测试直接调这个）。
  static Future<int> migrate(Directory from, Directory to) async {
    int copied = 0;
    if (!await from.exists()) return 0;
    await to.create(recursive: true);
    await for (final FileSystemEntity entity in from.list(recursive: true)) {
      final String rel = entity.path.substring(from.path.length);
      if (entity is Directory) {
        await Directory('${to.path}$rel').create(recursive: true);
        continue;
      }
      if (entity is! File) continue;
      final File dst = File('${to.path}$rel');
      await dst.parent.create(recursive: true);
      await entity.copy(dst.path);
      copied += 1;
    }
    return copied;
  }

  static File _file() => File('${_dir().path}${Platform.pathSeparator}$_fileName');

  static Future<Map<String, dynamic>> read() async {
    try {
      final f = _file();
      if (!await f.exists()) return <String, dynamic>{};
      final text = await f.readAsString();
      final decoded = jsonDecode(text);
      if (decoded is Map<String, dynamic>) return decoded;
      return <String, dynamic>{};
    } catch (_) {
      return <String, dynamic>{};
    }
  }

  /// 整份覆盖写（只有确实要「重来一份」时才用它；一般用 [merge]）。
  static Future<void> write(Map<String, dynamic> data) async {
    try {
      final dir = _dir();
      if (!await dir.exists()) await dir.create(recursive: true);
      await _file().writeAsString(jsonEncode(data), flush: true);
      lastWriteError = null;
    } catch (e) {
      // 写失败不阻塞界面：设置仍在本次运行内生效。
      // 但必须留下原因 —— 「过一会儿又要重新登录」的根因就是写不进去/被清掉，
      // 没有这行日志的话只能靠猜。
      lastWriteError = e.toString();
      // ignore: avoid_print
      print('LocalStore.write 失败（${_dir().path}）：$e');
    }
  }

  /// 只覆盖给定的键，其它键原样保留（读-改-写）。
  ///
  /// 2026-10-01 第二十一轮（用户报「切完微信、点进去闪退一下、图标又变回企鹅」）：
  /// `AppConfig._persist()` 原来是把**整份配置重写成它自己那 11 个键** ——
  /// 于是别人写进去的东西每隔一会儿就被抹掉，其中最要命的是两个：
  ///
  /// · 桌面身份的「已复位」标记丢了 -> 每次启动都重新「复位成 QQ」->
  ///   启动阶段又去切 alias -> 被 ROM 杀进程（用户看到的「闪退一下」），
  ///   而且桌面被拉回 QQ/企鹅；
  /// · 消息 resume 锚点丢了 -> 每次启动都被当成「本地没有锚点」->
  ///   服务端把全部历史补发一遍（那个「一进来 90 多条」的老问题会反复出现）。
  ///
  /// 这个文件是**多个模块共用**的（配置、resume 锚点、桌面身份标记…），
  /// 所以谁都不许整份覆盖，只能合并自己那几个键。
  static Future<void> merge(Map<String, dynamic> patch) async {
    final Map<String, dynamic> data = await read();
    data.addAll(patch);
    await write(data);
  }
}
