// 头像的本地文件存储（PRD 3.1.2：上传朋友的自拍作为 AI 头像；
// 用户 2026-09-27 追加：用户自己的头像也从相册选）。
//
// 为什么要单独一层：
// - 内置头像是 assets 里的只读资源，选中的是 avatar_id 字符串；
// - 从相册选的头像是运行期产生的字节，必须写到应用私有目录里才能重启后还在；
// - 所以配置里额外存一个“头像文件路径”，为空表示用内置头像。
//
// 两份头像各存各的文件，互不覆盖：
// - AI 头像：ai_avatar.jpg（设置页里给 AI 换头像）
// - 用户头像：user_avatar.jpg（首次安装 / 点头像修改）
// 早期只做 AI 头像时 save() 只有一个入口，现在保留 save() 的旧语义（= AI 头像），
// 新增 saveUser() 走用户头像，避免改动既有调用点。
//
// 不引入 path_provider：沿用 local_store.dart 已经验证过的 dart:io 目录策略，
// 使用 dart:io 管理目录：原生插件需要 Windows 开发者模式，多数环境无法开启。
//
// 尺寸：选图时原生侧已经把最长边压到 1024 / JPEG 70（见 MainActivity.kt），
// 头像只用于圆形展示，这里再原样落盘即可，不做二次压缩。

import 'dart:io';
import 'dart:typed_data';

import 'local_store.dart';

class AvatarStore {
  AvatarStore._();

  static const String _fileName = 'ai_avatar.jpg';
  static const String _userFileName = 'user_avatar.jpg';

  /// 保存给 AI 用的头像，返回文件绝对路径；失败返回 null。
  static Future<String?> save(Uint8List bytes) =>
      _write(bytes, _fileName);

  /// 保存用户自己的头像，返回文件绝对路径；失败返回 null。
  static Future<String?> saveUser(Uint8List bytes) =>
      _write(bytes, _userFileName);

  static Future<String?> _write(Uint8List bytes, String fileName) async {
    if (bytes.isEmpty) return null;
    try {
      final Directory dir = LocalStore.dir();
      if (!await dir.exists()) await dir.create(recursive: true);
      final File f = File('${dir.path}${Platform.pathSeparator}$fileName');
      await f.writeAsBytes(bytes, flush: true);
      return f.path;
    } catch (_) {
      return null;
    }
  }

  /// 自定义头像文件；没设置或文件已不存在时返回 null（调用方回退内置头像）。
  static File? fileOf(String path) {
    if (path.isEmpty) return null;
    try {
      final File f = File(path);
      return f.existsSync() ? f : null;
    } catch (_) {
      return null;
    }
  }

  /// 清掉用户自己的头像缓存文件（退出登录时调用）。
  ///
  /// 第六轮起用户头像是**服务端头像的本地缓存**：退出登录后再登录别的账号，
  /// 必须换成新账号的脸。不清掉的话会短暂看到上一个账号的头像，那是串数据。
  static Future<void> clearUser() async {
    try {
      final Directory dir = LocalStore.dir();
      final File f = File('${dir.path}${Platform.pathSeparator}$_userFileName');
      if (f.existsSync()) await f.delete();
    } catch (_) {
      // 删不掉不影响使用：配置里的路径已经清了，不会再去读它
    }
  }

  /// 清掉自定义头像（用户改回内置头像时调用）。
  static Future<void> clear(String path) async {
    final File? f = fileOf(path);
    if (f == null) return;
    try {
      await f.delete();
    } catch (_) {
      // 删不掉不影响使用，配置里已经不再引用它
    }
  }
}