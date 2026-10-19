// 桌面身份（图标 + 名字）的启动对账与切换。
//
// 用户 2026-10-01（第十八轮 b）原话：
//   「我都说了默认的名称和图标用QQ，图标是企鹅，但是安装后名称还是叫极迅，
//     图标用的是微信。微信应该是用户自己到设置里面切换然后名称也自己变成微信，
//     一定是要在手机桌面生效的」
//
// 为什么装完不是 QQ：
//   1. Android 把 activity-alias 的**启用状态跟着包一起记住**（升级安装也保留），
//      而 App 原来只在「设置 → 应用到手机桌面」那一下才切换 ——
//      他之前切过微信，所以升级回来桌面上还是微信；
//   2. 桌面上的名字来自 alias 的 label，而「设置 → 应用」里那个名字来自
//      application 的 label（原来是「极迅」），两者本来不是一回事。
//
// 现在的做法：
//   · 每次启动都对一次账：把 App 里记着的那一套身份重新应用到桌面（幂等）；
//   · **首次升级做一次性复位**：默认身份 = QQ（名字 QQ + 企鹅图标），
//     并把「本机选的是 QQ」同步到服务端，免得下次同步又把旧的改回来；
//   · 之后用户在设置里选什么就一直是什么（选微信 → 桌面名字和图标都变微信）。

import 'package:flutter/foundation.dart';

import '../config/app_assets.dart';
import '../config/app_config.dart';
import 'api_client.dart';
import 'launcher_icon.dart';
import 'local_store.dart';

class LauncherIdentityService {
  LauncherIdentityService._();

  /// 一次性复位的标记（存在本地设置文件里）。见过这个标记就不再复位。
  static const String kResetDoneKey = 'launcher_default_reset_qq';

  /// 算出这次启动该用哪一套身份。纯函数，方便直接测。
  ///
  /// [resetDone] = 之前已经做过一次性复位；[stored] = 本机记着的下标。
  static int resolveIndex({required bool resetDone, required int stored}) {
    if (!resetDone) return defaultIndex; // 首次：默认 QQ
    return isValidIndex(stored) ? stored : defaultIndex;
  }

  /// 默认那套（列表里的第一个 = QQ）。
  static int get defaultIndex => kLauncherIdentities.first.aliasIndex;

  /// 下标是不是有效的那两套之一。
  static bool isValidIndex(int index) {
    for (final LauncherIdentity id in kLauncherIdentities) {
      if (id.aliasIndex == index) return true;
    }
    return false;
  }

  /// 启动时调一次（main.dart）。返回最终生效的下标。
  ///
  /// 非 Android 平台什么都不做（桌面图标只能在 Android 真机上换）。
  static Future<int> ensureApplied() async {
    if (kIsWeb || defaultTargetPlatform != TargetPlatform.android) {
      return AppConfig.instance.launcherIndex;
    }

    bool resetDone = false;
    try {
      final Map<String, dynamic> data = await LocalStore.read();
      resetDone = data[kResetDoneKey] == true;
    } catch (_) {
      // 读不出来就当没复位过（复位本身幂等）。
    }

    final int index = resolveIndex(
      resetDone: resetDone,
      stored: AppConfig.instance.launcherIndex,
    );

    if (!resetDone) {
      final String label = launcherIdentityByIndex(index).label;
      // 名字跟着身份走：清单里 application 的 label 是编译期的「QQ」，
      // App 内部 / 任务卡片上显示的名字用配置里这个。
      await AppConfig.instance.update(launcherIndex: index, appName: label);
      // 标记用 merge 写（第二十一轮）：原来用 write 会把 AppConfig 刚写进去的
      // 身份/名字又盖回旧值 —— 于是标记和选择一起丢，下次启动又复位成 QQ。
      await LocalStore.merge(<String, dynamic>{kResetDoneKey: true});
      try {
        // 让服务端也记住，否则下次 sync 会把旧身份又同步回来。
        await ApiClient.instance.updateMe(launcherIndex: index, appName: label);
      } catch (_) {
        // 没登录 / 没网都无所谓：本机已经切好了。
      }
      // 一次性复位才允许在启动阶段动 alias（这是唯一一次必要的迁移）。
      //
      // 2026-10-01 第二十一轮（用户实测「点进去闪退一下，然后图标又变回 QQ」）：
      // 切换 alias 在部分 ROM 上会杀进程，所以**启动阶段一律不再切** ——
      // 桌面状态本来就该和本机记的一致（用户每次改都是走设置页那条路，
      // 而那条路会先落盘再切）。这样就不会再出现「一打开就闪退」。
      await LauncherIcon.apply(index);
      return index;
    }

    // 之后每次启动只做「不动 alias」的对账：把 App 内部/任务卡片上的名字
    // 跟本机记着的那套身份对齐（纯本地写，不会杀进程）。
    final LauncherIdentity identity = launcherIdentityByIndex(
      LauncherIdentityService.isValidIndex(AppConfig.instance.launcherIndex)
          ? AppConfig.instance.launcherIndex
          : defaultIndex,
    );
    if (AppConfig.instance.appName != identity.label) {
      await AppConfig.instance.update(appName: identity.label);
    }
    return identity.aliasIndex;
  }
}
