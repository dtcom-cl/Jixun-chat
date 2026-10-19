// 桌面图标 / 桌面名称切换（用户 2026-09-25 要求“软件图标可自定义”）。
//
// Android 的 android:label 与 android:icon 是编译期资源，运行时无法改成任意文本。
// 所以 AndroidManifest.xml 里预置了 6 套 activity-alias，每套带自己的图标和名称；
// 这里通过 MethodChannel 调原生 PackageManager 切换启用哪一套。
//
// 非 Android 平台直接返回 false，界面提示“当前平台不支持”。

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';

class LauncherIcon {
  LauncherIcon._();

  static const MethodChannel _channel = MethodChannel('jixun/launcher');

  /// 当前桌面用的是第几套身份（1 起）；问不到返回 0。
  ///
  /// 2026-10-01 第二十轮：切换 alias 的启用状态在部分 ROM 上会**杀掉应用进程**
  /// （用户实测「切换后闪退、进去之后图标又变回企鹅」）。所以启动对账前先问一句，
  /// 已经是目标那一套就什么都不做 —— 从根子上少杀几次。
  static Future<int> current() async {
    if (kIsWeb || defaultTargetPlatform != TargetPlatform.android) return 0;
    try {
      final int? index = await _channel.invokeMethod<int>('currentLauncher');
      return index ?? 0;
    } on PlatformException {
      return 0;
    } on MissingPluginException {
      return 0;
    }
  }

  /// 切换成第 [aliasIndex] 套桌面身份（1 起，对应 kLauncherIdentities）。
  ///
  /// 返回 true 表示原生侧已成功切换（**已经是这一套时也返回 true**）。
  static Future<bool> apply(int aliasIndex) async {
    if (kIsWeb || defaultTargetPlatform != TargetPlatform.android) {
      return false;
    }
    try {
      final bool? ok = await _channel
          .invokeMethod<bool>('setLauncher', <String, dynamic>{'index': aliasIndex});
      return ok ?? false;
    } on PlatformException catch (e) {
      debugPrint('切换桌面图标失败：${e.message}');
      return false;
    } on MissingPluginException {
      debugPrint('切换桌面图标失败：原生方法未注册');
      return false;
    }
  }
}
