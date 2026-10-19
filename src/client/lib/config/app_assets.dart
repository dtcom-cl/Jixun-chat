// 本地资源目录（用户 2026-09-25 要求：头像与图标全部走本地资源，禁止网络加载）。
//
// 三类资源：
// - 表情 SVG：assets/emoji/*.svg，共 20 个，见 app_emoji.dart；
// - AI 头像：assets/avatar/*.png，内置 6 个；
// - 软件图标：assets/app_icon/*.png（目前只剩两张，
//   已替换为空白透明占位图，由开发者自行替换）。
//
// 另外，桌面图标与桌面名称在 Android 上无法用任意文本在运行时修改
// （android:label / android:icon 是编译期资源），所以用 activity-alias 预置了
// **两套**“启动器身份”（QQ / 微信），见 launcherIdentities。

/// 一个可选的 AI 头像（本地 PNG）。
class AvatarAsset {
  const AvatarAsset(this.id, this.asset, this.label);

  final String id;
  final String asset;
  final String label;
}

/// 一个可选的软件图标（本地 PNG）。
class IconAsset {
  const IconAsset(this.id, this.asset, this.label);

  final String id;
  final String asset;
  final String label;
}

/// 一套“启动器身份”：桌面图标 + 桌面名称。
///
/// [aliasIndex] 对应 AndroidManifest.xml 里的 activity-alias 下标（1 起）。
class LauncherIdentity {
  const LauncherIdentity(this.aliasIndex, this.label);

  final int aliasIndex;
  final String label;
}

const String kDefaultAvatarId = 'avatar_1';
const String kDefaultIconId = 'app_icon';

const List<AvatarAsset> kAvatarAssets = <AvatarAsset>[
  AvatarAsset('avatar_1', 'assets/avatar/avatar_1.png', '头像 1'),
  AvatarAsset('avatar_2', 'assets/avatar/avatar_2.png', '头像 2'),
  AvatarAsset('avatar_3', 'assets/avatar/avatar_3.png', '头像 3'),
  AvatarAsset('avatar_4', 'assets/avatar/avatar_4.png', '头像 4'),
  AvatarAsset('avatar_5', 'assets/avatar/avatar_5.png', '头像 5'),
  AvatarAsset('avatar_6', 'assets/avatar/avatar_6.png', '头像 6'),
];

const List<IconAsset> kIconAssets = <IconAsset>[
  IconAsset('app_icon', 'assets/app_icon/app_icon.png', '极迅'),
  IconAsset('icon_2', 'assets/app_icon/icon_2.png', '计算机'),
];

/// 桌面图标 + 桌面名称的组合。
///
/// 说明：Android 的 android:label 与 android:icon 是编译期资源，运行时无法改成
/// 任意文本，所以这里提供预置身份，覆盖“伪装成普通软件”的需求。
/// 软件名称（App 内部显示的名字）不在此限制内，可自由填写。
///
const List<LauncherIdentity> kLauncherIdentities = <LauncherIdentity>[
  LauncherIdentity(1, '极迅'),
  LauncherIdentity(2, '计算机'),
];

/// 按 id 找头像资源；找不到返回 null（调用方回退到默认头像）。
String? avatarAssetById(String id) {
  for (final AvatarAsset a in kAvatarAssets) {
    if (a.id == id) return a.asset;
  }
  return null;
}

/// 按 id 找图标资源；找不到返回 null。
String? iconAssetById(String id) {
  for (final IconAsset a in kIconAssets) {
    if (a.id == id) return a.asset;
  }
  return null;
}

/// 按 alias 下标找启动器身份；找不到返回默认（下标 1）。
LauncherIdentity launcherIdentityByIndex(int index) {
  for (final LauncherIdentity i in kLauncherIdentities) {
    if (i.aliasIndex == index) return i;
  }
  return kLauncherIdentities.first;
}
