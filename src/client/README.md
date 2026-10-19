# client 文件夹（客户端）

Flutter 手机 App，负责显示聊天界面、连接服务端、把多段消息依次呈现。

## 当前状态

已实现：聊天界面骨架、WebSocket 连接与自动重连、ACK 确认、断线补发、本地消息队列、打字机多段弹出、未读小红点、时间戳、设置模块。

## 怎么启动

需要 Flutter SDK。确认 `flutter` 已在 PATH 中，否则先把 SDK 的 `bin` 目录加入：

```powershell
$env:Path = '<Flutter SDK 路径>\bin;' + $env:Path
```

### 在手机上跑

前提：已安装 Android SDK，手机开启 USB 调试或已启动模拟器。

```powershell
cd src/client
flutter pub get
flutter run
```

### 在电脑上跑

无需 Android SDK。

```powershell
cd src/client
flutter run -d windows
```

### 自检

```powershell
flutter analyze
flutter test
```

## 服务器地址怎么填

默认值按平台区分，见 `lib/config/app_config.dart`：

- Windows 桌面端：`ws://127.0.0.1:8000/ws`
- Android 模拟器：`ws://10.0.2.2:8000/ws`
- 真机：在 App 设置页改成电脑的局域网 IP，例如 `ws://192.168.x.x:8000/ws`

## 各个文件干什么

- `lib/main.dart` 应用入口，读取设置、连接服务端、进入消息列表页
- `lib/config/app_config.dart` 配置项：服务器地址、软件名、AI 昵称、头像、图标
- `lib/services/local_store.dart` 设置的本地持久化，直接读写 settings.json
- `lib/models/message.dart` 消息数据结构，含 content 数组拆段
- `lib/models/conversation.dart` 会话数据结构
- `lib/services/ws_client.dart` WebSocket 客户端：连接、重连、ACK、断线补发、心跳
- `lib/services/local_queue.dart` 本地消息队列与未读管理
- `lib/utils/typewriter.dart` 打字机引擎，控制多段消息的呈现间隔
- `lib/ui/home_page.dart` 消息列表页，显示会话与未读标记
- `lib/ui/chat_page.dart` 聊天页，显示气泡、时间戳与输入框
- `lib/ui/settings_page.dart` 设置页
- `lib/ui/widgets/bubble.dart` 聊天气泡组件
- `lib/ui/widgets/input_bar.dart` 底部输入栏组件
- `lib/ui/widgets/red_dot.dart` 小红点组件

## 依赖

`pubspec.yaml`：

```yaml
dependencies:
  flutter:
    sdk: flutter
  web_socket_channel: ^3.0.1
  intl: ^0.20.2
```

### 与原始依赖清单的偏差

原清单中的 `provider` 与 `shared_preferences` 均未使用：

- `provider`：设置与消息状态由 Flutter 自带的 `ChangeNotifier` 与 `AnimatedBuilder` 承担，无需额外依赖
- `shared_preferences`：属于原生插件，Windows 上构建含插件的工程需要开启系统开发者模式。这里改用 `lib/services/local_store.dart` 直接读写 JSON 文件，改完生效、重启仍在，构建也不再依赖开发者模式

如需改回 `shared_preferences`，先开启开发者模式（设置 → 系统 → 开发者选项 → 开发人员模式）。
