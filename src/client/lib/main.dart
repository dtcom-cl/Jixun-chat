// 应用入口：初始化应用、搭建主界面骨架并建立 WebSocket 连接。
//
// 依据： ~ 3.6。
//
// 启动顺序：
//   读取本地设置 -> 应用 AI 昵称 -> 建立 WebSocket 连接 -> 进入消息列表页。
// 连接建立后：发送 resume（携带最后收到的 msg_id），并处理服务端推送。
//
// 2026-09-25 改动：
// - 服务器地址在连接前就做规范化（补 ws://、补 /ws）；
// - 设置页改完地址点“保存并重连”后，onConnected 会重新发 resume。
//

import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'config/app_config.dart';
import 'services/launcher_identity.dart';
import 'services/local_queue.dart';
import 'services/local_store.dart';
import 'services/session.dart';
import 'services/ws_client.dart';
import 'ui/auth_page.dart';
import 'ui/home_page.dart';

/// 全局 SnackBar 入口。
///
/// 服务端在“这个会话还没配 API / Key 无效”时会回一条 error（WS-001 4001~5001
/// 之外的 5002，见 ws_gateway.ERR_AI_NOT_CONFIGURED）。用户 2026-09-27 明确要求
/// “必须是真的 AI 对话，不要用固定脚本”，所以这种错误必须**当面告诉他**，
/// 不能只在日志里躺着——否则用户只会看到 AI 不回话，完全不知道是没填 Key。
final GlobalKey<ScaffoldMessengerState> kMessengerKey =
    GlobalKey<ScaffoldMessengerState>();

void _showGlobalError(String text) {
  final ScaffoldMessengerState? m = kMessengerKey.currentState;
  if (m == null) return;
  m
    ..hideCurrentSnackBar()
    ..showSnackBar(SnackBar(
      content: Text(text),
      duration: const Duration(seconds: 6),
    ));
}

/// 系统栏样式：底部导航栏用 App 自己的浅灰（不透明）+ 深色图标。
///
/// 2026-10-01 第十八轮 b：用户反馈底部那条黑边没修好。
/// 原因之一是原来把导航栏设成**透明**、指望 App 背景透上来，而部分 ROM
/// （尤其深色模式）根本不理透明请求，直接用自己的黑。改成不透明浅色之后，
/// 无论系统支不支持透明，那一条都是 App 的浅灰。
const SystemUiOverlayStyle kAppSystemUiStyle = SystemUiOverlayStyle(
  statusBarColor: Colors.transparent,
  statusBarIconBrightness: Brightness.dark,
  statusBarBrightness: Brightness.light,
  systemNavigationBarColor: kAppBottomBarColor,
  systemNavigationBarDividerColor: kAppBottomBarColor,
  systemNavigationBarIconBrightness: Brightness.dark,
  // 系统不要再自己给导航栏垫一层半透明底（那层在有些 ROM 上就是黑的）。
  systemNavigationBarContrastEnforced: false,
);

/// 底部那一条的颜色（与聊天页输入栏、标题栏同色）。
const Color kAppBottomBarColor = Color(0xFFF7F7F7);

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  // 2026-09-30 第十六轮（用户报告 UI 底部一条黑缺口）：让界面铺满整屏
  // （edge-to-edge），系统栏设成透明 + 深色图标。这样最底下那一条就由
  // App 自己的浅色背景填充（见 JiXunApp 里的 builder），不再是黑的。
  await SystemChrome.setEnabledSystemUIMode(SystemUiMode.edgeToEdge);
  // 2026-10-01 第十八轮 b（用户回报「软件底部的黑色还是没有兼容」）：
  // 原来这里把导航栏设成**透明**，指望下面的 App 背景透上来 ——
  // 有些 ROM（深色模式下尤其）不理透明请求，直接用自己的黑色，
  // 于是底下还是一条黑。现在给一个**不透明的浅色**：就算系统不支持透明，
  // 那一条也是 App 自己的浅灰，不会黑。
  SystemChrome.setSystemUIOverlayStyle(kAppSystemUiStyle);
  // 2026-10-01 第十七轮（用户报告「退出软件过一会儿不用又要重新登录」）：
  // 先把本地存储目录定下来（Android 上从缓存目录换到应用私有 files 目录，
  // 并把老配置搬过去），再读配置 —— 顺序不能反，否则读到的还是老目录。
  await LocalStore.init();
  await AppConfig.instance.load();
  LocalQueue.instance.applyConfig();
  _wireWebSocket();
  // 令牌失效（被别处登录挤下线 / 服务端换了密钥）时自动清凭证回登录页。
  Session.wireUnauthorizedHandler();

  // 只有已登录才去连服务端 —— 没登录时连 WS 会被服务端 1008 关掉，
  // 白白刷一屏失败日志，用户还以为是网络坏了。
  if (AppConfig.instance.isLoggedIn) {
    // 2026-09-28 第八轮：冷启动必须先把上次的 resume 锚点读回来，
    // 再连 WS。否则 onConnected 里 sendResume('') 会让服务端把
    // 「该用户全部消息」重发一遍 —— 历史重复插入、顺序错乱，
    // 而离线期间到期的延迟消息反而接不上。
    await LocalQueue.instance.restoreLastMsgId();
    WsClient.instance.connect();
    // 会话列表走 REST（PRD 3.4.1 多会话 + 3.1.6 未读）；失败只影响列表，不影响 WS。
    LocalQueue.instance.refreshConversations();
    // 顺带把服务端资料同步下来（昵称/头像/软件名等），失败不阻塞启动。
    unawaited(Session.instance.sync());
  }

  runApp(const JiXunApp());

  // 有些 ROM 会在首帧之后把系统栏样式重置回自己的默认值（底部又变黑），
  // 这里再补一次；成本极低，但能挡住这一类「明明设了又变回去」。
  WidgetsBinding.instance.addPostFrameCallback((_) {
    SystemChrome.setSystemUIOverlayStyle(kAppSystemUiStyle);
  });

  unawaited(LauncherIdentityService.ensureApplied());
}

/// 把 WS 客户端与服务端消息处理接起来（WS-001 3.3 / 3.5 / 3.6 / 3.8）。
void _wireWebSocket() {
  final WsClient ws = WsClient.instance;
  final LocalQueue queue = LocalQueue.instance;

  ws.onConnected = () {
    // 连接成功后发送 resume，携带最后收到的 msg_id。
    // 锚点是落盘的（LocalQueue.restoreLastMsgId），所以冷启动重连也带得上。
    ws.sendResume(queue.lastMsgId);
  };

  ws.onServerMessage = (Map<String, dynamic> json) {
    switch (json['type']?.toString()) {
      case 'message':
        // 收到 message 后必须回 ack（WS-001 一、协议总则）
        queue.ingestServerMessage(json);
        final String msgId = json['msg_id']?.toString() ?? '';
        if (msgId.isNotEmpty) ws.sendAck(msgId);
        break;
      case 'resume_missed':
        final dynamic list = json['messages'];
        if (list is List) {
          queue.ingestResumeMissed(list);
          // 补发消息逐条 ack（WS-001 3.6）
          for (final dynamic item in list) {
            if (item is Map && item['msg_id'] != null) {
              ws.sendAck(item['msg_id'].toString());
            }
          }
        }
        break;
      case 'error':
        final String code = json['code']?.toString() ?? '';
        final String message = json['message']?.toString() ?? '未知错误';
        queue.setError('$code: $message');
        // 5002 = AI 没配置/全部配置失败：必须弹出来，用户才知道要去会话设置里填。
        _showGlobalError(code == '5002' ? message : '$code: $message');
        break;
      default:
        break;
    }
  };
}

class JiXunApp extends StatelessWidget {
  const JiXunApp({super.key});

  @override
  Widget build(BuildContext context) {
    return AnimatedBuilder(
      animation: AppConfig.instance,
      builder: (BuildContext context, _) {
        return MaterialApp(
          scaffoldMessengerKey: kMessengerKey,
          title: AppConfig.instance.appName,
          debugShowCheckedModeBanner: false,
          theme: ThemeData(
            useMaterial3: true,
            scaffoldBackgroundColor: Colors.white,
            colorScheme: ColorScheme.fromSeed(seedColor: const Color(0xFF0099FF)),
          ),
          builder: (BuildContext context, Widget? child) => ColoredBox(
            color: kAppBottomBarColor,
            child: SafeArea(
              top: false,
              child: child ?? const SizedBox.shrink(),
            ),
          ),
          home: const _RootGate(),
        );
      },
    );
  }
}

/// 首屏分流：没登录就去注册/登录页，登录了才进消息列表。
///
/// 用户 2026-09-27 第六轮要求「第 1 次使用叫做注册，需要输入头像、昵称、密码」，
/// 注册/登录成功后拿到服务端分配的 4 位账号 ID 与永久令牌，
/// 「换手机删除软件之后重新下回来登陆上还是之前的配置」。
///
/// 这里刻意**不用路由跳转**，而是让根节点根据登录态直接返回不同的页面组件：
/// 登录成功会 notifyListeners，外层 AnimatedBuilder 重建，
/// 首屏自动从 AuthPage 变成 HomePage；被挤下线时反向切回 AuthPage。
///
/// 为什么不用 Navigator：
/// MaterialApp.home 生成的只是初始路由，之后改变 home 并不会让 Navigator
/// 重新生成已有路由；如果写成“home 视条件返回不同页面 + 在引导页 pop”，
/// 要么卡在引导页，要么把唯一的路由 pop 掉变成黑屏。
/// 直接把首屏当作受登录态驱动的普通组件，行为最确定。
class _RootGate extends StatelessWidget {
  const _RootGate();

  @override
  Widget build(BuildContext context) {
    // 两个可监听对象：AppConfig（凭证）与 Session（登录中/冷却倒计时）。
    return AnimatedBuilder(
      animation: Listenable.merge(<Listenable>[
        AppConfig.instance,
        Session.instance,
      ]),
      builder: (BuildContext context, _) {
        if (!AppConfig.instance.isLoggedIn) {
          return const AuthPage();
        }
        return const HomePage();
      },
    );
  }
}
