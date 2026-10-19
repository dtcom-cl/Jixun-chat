// WebSocket 客户端：连接、确认、断线重连与补发。
//
// 职责：维护与服务端的 WebSocket 长连接，处理连接、断开、重连、心跳。
// 协议：只收发文档定义的 7 种消息类型，不新增。
//
// 2026-09-25 修复“填入地址后仍显示未连接”的问题，改动点：
// 1. connect(force: true) 允许强制重连，设置页“按新地址重连”不再被
//    旧的 connecting/connected 状态挡住（原来 connect() 会直接 return）。
// 2. 连接加 8 秒超时。原来 channel.ready 在部分网络下会长时间挂起，
//    界面一直停在“未连接”，用户看不到任何反馈。
// 3. 失败原因写进 lastErrorDetail，设置页直接显示，便于定位是地址错、
//    服务端没开、还是手机与电脑不在同一网段。
// 4. 断开旧连接时先解绑回调再关闭，避免旧连接的 onDone 触发一次多余的重连，
//    与新连接互相踩踏。
// 5. 地址统一经 AppConfig.normalizeServerUrl 规范化（补 ws://、补 /ws）。

import 'dart:async';
import 'dart:convert';
import 'dart:math';

import 'package:flutter/foundation.dart';
import 'package:web_socket_channel/web_socket_channel.dart';

import '../config/app_config.dart';

enum WsStatus { disconnected, connecting, connected }

class WsClient extends ChangeNotifier {
  WsClient._();

  static final WsClient instance = WsClient._();

  static const int _heartbeatSeconds = 30; // WS-001 一：客户端每 30 秒发一次
  static const int _reconnectBaseSeconds = 3; // WS-001 一：断线后 3 秒重连
  static const int _reconnectMaxSeconds = 30; // 指数退避上限 30 秒
  static const Duration _connectTimeout = Duration(seconds: 8);

  WebSocketChannel? _channel;
  StreamSubscription<dynamic>? _sub;
  Timer? _heartbeatTimer;
  Timer? _reconnectTimer;
  int _retryCount = 0;
  bool _manualClose = false;

  WsStatus _status = WsStatus.disconnected;
  WsStatus get status => _status;
  bool get isConnected => _status == WsStatus.connected;
  bool get isConnecting => _status == WsStatus.connecting;

  /// 当前正在尝试连接的地址，设置页用来显示“正在连 ws://...”。
  String currentUrl = '';

  /// 最近一次失败原因（原始异常文本）。
  String? lastError;

  /// 给人看的失败原因。
  String? lastErrorDetail;

  /// 本次「断线周期」里有没有试过切换到云端地址（避免来回跳）。
  ///
  /// 2026-10-02 第二十九轮（用户改用 Cloudflare Tunnel）：快速隧道的网址每次重启都会变，
  /// 没法写死在包里；改成电脑通过局域网发现把**当前**云端地址告诉手机（存进 AppConfig.cloudUrl）。
  /// 局域网地址连不上时（说明出门了 / 换了网络），这里自动切到云端地址再试一次。
  bool _cloudTried = false;

  /// 只影响「这一次连接」的地址覆盖（目前只有云端地址会用到）。
  ///
  /// 为什么不直接改 AppConfig.serverUrl：那样会把「家里的局域网地址」永久换掉，
  /// 以后回到家反而要绕公网。覆盖只在内存里生效，成功/失败后都会还原。
  String? _urlOverride;

  /// 连接尝试的「代次」。每发起一次 `_open()` 或调用 `_teardown()` 都会 +1；
  /// 一次尝试回来时如果发现代次已经变了，说明它已经过期，必须把刚建的连接关掉。
  ///
  /// 2026-10-02（用户实测「客户端连上了一直掉线」）：没有这个保护时，
  /// 「局域网超时 → 自动切云端」的两条连接会同时存在，而服务端对同一个用户
  /// 只保留最新一条（旧的被 1000 关掉）→ 两条互相踹 → 死循环。
  int _openGeneration = 0;

  /// 连接建立后回调（上层用它发 resume）。
  void Function()? onConnected;

  /// 收到服务端消息回调（message / resume_missed / heartbeat / error）。
  void Function(Map<String, dynamic> json)? onServerMessage;

  /// 生成 client_msg_id，格式 cli_{毫秒时间戳}_{6位随机}（WS-001 七）。
  static String newClientMsgId() {
    final int ts = DateTime.now().millisecondsSinceEpoch;
    const String chars = 'abcdefghijklmnopqrstuvwxyz0123456789';
    final Random rnd = Random();
    final String suffix =
        List<String>.generate(6, (_) => chars[rnd.nextInt(chars.length)]).join();
    return 'cli_${ts}_$suffix';
  }

  // ------------------------------------------------------------ 连接

  /// 建立连接。
  ///
  /// [force] 为 true 时先彻底断开再重连（设置页“按新地址重连”用），
  /// 否则若已在连接中/已连接则直接返回。
  void connect({bool force = false}) {
    if (force) {
      _teardown();
    } else if (_status == WsStatus.connecting || _status == WsStatus.connected) {
      return;
    }
    _manualClose = false;
    _retryCount = 0;
    _reconnectTimer?.cancel();
    _reconnectTimer = null;
    unawaited(_open());
  }

  /// 彻底断开且不自动重连。
  void disconnect() {
    _manualClose = true;
    _teardown();
    lastErrorDetail = null;
    _setStatus(WsStatus.disconnected);
  }

  /// 关闭底层资源，但不动 _status（调用方决定后续状态）。
  void _teardown() {
    // 让「还在路上」的那次 _open 作废：它回来时会自己把刚建好的连接关掉。
    // （不加这个，切地址时会同时存在两条连接 —— 服务端同用户只留最新一条，
    //   两条就会互相把对方踹下线，表现成「一直连上又掉线」的死循环。）
    _openGeneration++;
    _reconnectTimer?.cancel();
    _reconnectTimer = null;
    _heartbeatTimer?.cancel();
    _heartbeatTimer = null;

    // 先解绑再关闭：否则旧连接的 onDone 会触发一次多余的重连。
    final StreamSubscription<dynamic>? sub = _sub;
    final WebSocketChannel? ch = _channel;
    _sub = null;
    _channel = null;
    sub?.cancel();
    try {
      ch?.sink.close();
    } catch (_) {
      // 关闭失败无所谓，连接已经不可用
    }
  }

  void _setStatus(WsStatus s) {
    if (_status == s) return;
    _status = s;
    notifyListeners();
  }

  Future<void> _open() async {
    // 2026-10-02（用户实测「客户端连上了一直掉线」）：给每次连接尝试打上「代次」。
    // 当上层在这条连接还没建好时又发起了一次连接（比如局域网失败自动切云端地址），
    // 旧的那次回来时必须**自己关掉**，否则会留下第二条连接 ——
    // 服务端对同一个用户只保留最新一条（旧的会被 1000 关掉），两条就会互相踹，
    // 表现就是客户端「连上—掉线—再连上」的死循环。
    final int gen = ++_openGeneration;
    final String base = AppConfig.normalizeServerUrl(_urlOverride ?? AppConfig.instance.serverUrl);
    // 第六轮：WebSocket 也要鉴权。令牌只能挂在连接地址的查询参数上——
    // WS-001 把消息类型写死成 7 种，不能为了登录再加第 8 种。
    // 服务端 /ws 路由接 `token` 查询参数，验不过直接 1008 关闭。
    final String token = AppConfig.instance.token;
    final String url = token.isEmpty
        ? base
        : '$base${base.contains('?') ? '&' : '?'}token=${Uri.encodeQueryComponent(token)}';
    currentUrl = url;
    lastErrorDetail = null;
    _setStatus(WsStatus.connecting);

    if (base.isEmpty) {
      _fail('服务器地址为空', '请到设置页填写，例如 ws://192.168.x.x:8000/ws');
      return;
    }
    if (token.isEmpty) {
      // 没登录就别去连了：服务端会 1008 关掉，白白刷一屏失败日志。
      _fail('还没登录', '请先注册或登录账号');
      return;
    }
    final Uri? uri = Uri.tryParse(url);
    if (uri == null || uri.host.isEmpty) {
      _fail('地址格式不对：$url', '正确写法：ws://192.168.x.x:8000/ws');
      return;
    }

    try {
      final WebSocketChannel channel = WebSocketChannel.connect(uri);
      await channel.ready.timeout(_connectTimeout);
      if (gen != _openGeneration) {
        // 这次尝试已经过期（期间又发起了新的连接 / 被手动断开）：
        // 把刚建好的这条**主动关掉**，绝不留下第二条连接。
        try {
          await channel.sink.close();
        } catch (_) {
          // 关不掉也无所谓，进程里不会再用它
        }
        return;
      }
      _channel = channel;
      _sub = channel.stream.listen(
        (dynamic raw) => _handleRaw(raw, gen),
        onError: (Object e) {
          // 只有「当前这条」连接的错误才处理：旧连接被服务端顶掉时报的错
          // 会把新连接一起拆掉，形成「连上—掉线」死循环（2026-10-02 实测）。
          if (gen == _openGeneration) _fail('连接中断', e.toString());
        },
        onDone: () => _handleDone(gen),
        cancelOnError: true,
      );
      _retryCount = 0;
      _cloudTried = false; // 连上了：下次断线还能再试云端地址
      _urlOverride = null; // 覆盖只在「这一次尝试」里有效，连上后就还原成保存的地址
      lastError = null;
      lastErrorDetail = null;
      _setStatus(WsStatus.connected);
      _startHeartbeat();
      onConnected?.call();
    } on TimeoutException {
      if (gen != _openGeneration) return; // 过期尝试的报错不要再改状态/触发重连
      _fail(
        '连接超时（8 秒）',
        '电脑服务端可能没启动，或被防火墙挡住。\n'
            '检查：1) 电脑上双击 start.bat 是否在运行；'
            '2) 手机和电脑是否同一个 Wi-Fi；'
            '3) 地址是否写成 ws://电脑IP:8000/ws。',
      );
    } catch (e) {
      if (gen != _openGeneration) return;
      _fail('连接失败', _explain(e, url));
    }
  }

  /// 把底层异常翻译成用户能看懂的话。
  String _explain(Object e, String url) {
    final String raw = e.toString();
    if (raw.contains('Connection refused') || raw.contains('errno = 111')) {
      return '电脑拒绝了连接（$url）。多半是服务端没启动，或端口不是 8000。';
    }
    if (raw.contains('Failed host lookup') || raw.contains('nodename')) {
      return '找不到这个地址（$url）。检查 IP 是否写错、手机和电脑是否同一 Wi-Fi。';
    }
    if (raw.contains('Network is unreachable')) {
      return '网络不可达。检查手机是否连着 Wi-Fi。';
    }
    if (raw.contains('Handshake') || raw.contains('HTTP/1.1')) {
      return '握手失败（$url）。确认服务端路径是 /ws，例如 ws://192.168.x.x:8000/ws。';
    }
    return raw;
  }

  void _fail(String title, String detail) {
    final bool wasCloudAttempt = _urlOverride != null;
    lastError = title;
    lastErrorDetail = detail;
    _teardown();
    _setStatus(WsStatus.disconnected);
    if (wasCloudAttempt) {
      // 云端地址也没连上：下一次回到保存的地址（局域网）再试，
      // 并且本次周期不再反复横跳（_cloudTried 保持 true，成功时才会重置）。
      _urlOverride = null;
    }
    notifyListeners();
    if (_manualClose) return;
    if (!wasCloudAttempt && _switchToCloudIfPossible()) return;
    _scheduleReconnect();
  }

  /// 当前地址连不上时，改用电脑给的**云端地址**（Cloudflare 隧道）再连一次。
  ///
  /// 返回 true 表示已经切过去并开始重连（调用方就不要再排普通重连了）。
  /// 只在「这一轮还没试过」且「云端地址确实不同」时才动手，避免来回跳。
  bool _switchToCloudIfPossible() {
    if (_cloudTried) return false;
    final String cloud = AppConfig.instance.cloudUrl.trim();
    if (cloud.isEmpty) return false;
    final String current = AppConfig.normalizeServerUrl(AppConfig.instance.serverUrl);
    if (cloud == current) return false;
    _cloudTried = true;
    _urlOverride = cloud;
    lastErrorDetail = '${lastErrorDetail ?? ''}\n\n局域网地址连不上，正在改用云端地址：$cloud';
    notifyListeners();
    connect(force: true);
    return true;
  }

  void _handleDone(int gen) {
    // 旧连接（已被新连接顶掉 / 已被手动断开）关闭时不要动当前连接：
    // 否则新连接会被旧连接的 onDone 拆掉 → 「连上又掉线」死循环。
    if (gen != _openGeneration) return;
    if (_manualClose) {
      _teardown();
      _setStatus(WsStatus.disconnected);
      return;
    }
    lastErrorDetail = '连接已断开，正在自动重连…';
    _teardown();
    _setStatus(WsStatus.disconnected);
    notifyListeners();
    _scheduleReconnect();
  }

  void _scheduleReconnect() {
    if (_manualClose) return;
    _reconnectTimer?.cancel();
    final int seconds = (_reconnectBaseSeconds * (1 << _retryCount.clamp(0, 4)))
        .clamp(_reconnectBaseSeconds, _reconnectMaxSeconds);
    _retryCount++;
    _reconnectTimer = Timer(Duration(seconds: seconds), () {
      if (_manualClose) return;
      unawaited(_open());
    });
  }

  void _handleRaw(dynamic raw, int gen) {
    if (gen != _openGeneration) return; // 旧连接（已被顶掉）的消息一律丢弃
    if (raw is! String) return;
    Map<String, dynamic> json;
    try {
      final dynamic decoded = jsonDecode(raw);
      if (decoded is! Map<String, dynamic>) return;
      json = decoded;
    } catch (_) {
      return;
    }
    final String? type = json['type']?.toString();
    if (type == 'heartbeat') return; // 收到服务端 heartbeat，不需要回复（WS-001 3.7）
    onServerMessage?.call(json);
  }

  // ------------------------------------------------------------ 发送

  void _send(Map<String, dynamic> payload) {
    final WebSocketChannel? ch = _channel;
    if (ch == null || _status != WsStatus.connected) return;
    payload.putIfAbsent('timestamp', () => _nowIso());
    try {
      ch.sink.add(jsonEncode(payload));
    } catch (e) {
      lastErrorDetail = '发送失败：$e';
      notifyListeners();
    }
  }

  /// 发送用户消息（WS-001 3.2 send）。
  ///
  /// [contentType] 取 text / image / voice；图片时 [content] 传 data URL。
  void sendUserMessage({
    required int conversationId,
    required String clientMsgId,
    required String content,
    String contentType = 'text',
  }) {
    _send(<String, dynamic>{
      'type': 'send',
      'client_msg_id': clientMsgId,
      'conversation_id': conversationId,
      'content': content,
      'content_type': contentType,
    });
  }

  /// 兼容旧调用名。
  void sendUserText({
    required int conversationId,
    required String clientMsgId,
    required String content,
  }) {
    sendUserMessage(
      conversationId: conversationId,
      clientMsgId: clientMsgId,
      content: content,
    );
  }

  /// 确认收到消息（WS-001 3.4 ack）。
  void sendAck(String msgId) {
    _send(<String, dynamic>{'type': 'ack', 'msg_id': msgId});
  }

  /// 请求补发遗漏消息（WS-001 3.5 resume）。
  void sendResume(String lastMsgId) {
    _send(<String, dynamic>{'type': 'resume', 'last_msg_id': lastMsgId});
  }

  void _startHeartbeat() {
    _heartbeatTimer?.cancel();
    _heartbeatTimer =
        Timer.periodic(const Duration(seconds: _heartbeatSeconds), (_) {
      _send(<String, dynamic>{'type': 'heartbeat'});
    });
  }

  static String _nowIso() {
    final DateTime now = DateTime.now();
    final Duration off = now.timeZoneOffset;
    final String sign = off.isNegative ? '-' : '+';
    final String hh = off.inHours.abs().toString().padLeft(2, '0');
    final String mm = (off.inMinutes.abs() % 60).toString().padLeft(2, '0');
    String two(int v) => v.toString().padLeft(2, '0');
    return '${now.year}-${two(now.month)}-${two(now.day)}T'
        '${two(now.hour)}:${two(now.minute)}:${two(now.second)}$sign$hh:$mm';
  }

  @override
  void dispose() {
    disconnect();
    super.dispose();
  }
}