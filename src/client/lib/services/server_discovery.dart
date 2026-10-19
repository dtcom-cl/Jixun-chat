// 局域网自动发现：让 App 自己找到电脑（不用手填 IP）。
//
// 用户 2026-10-01 第二十八轮原话：
//   「我现在用的就是热点啊 但还是没用，刚刚我才试过，只有本地IPV4可以 公网谁都用不了」
//
// 背景：电脑挂在**手机热点**下时（192.168.43.x），热点按设计不允许入站、运营商又是
// CGNAT —— 公网入站在这个环境下不可能。但热点下「手机和电脑本来就在同一个网里」，
// 局域网直连一直好用（用户自己验证过）。真正的痛点是：**电脑 IP 一变
// （换网络、重连热点、路由器重新分配），App 里就得手改地址**。
//
// 做法：App 往局域网广播一个探测包，电脑上的服务端（src/server/discovery.py）回一句
// 「我在这儿、端口是多少」，App 据此自动填好地址。全程本地 UDP 广播 ——
// 不经过任何第三方服务器、不需要账号、不需要公网。
//
// 协议（必须与 src/server/discovery.py 保持一致）：
//   探测：正文包含 ZUICHENG_PROBE
//   应答：ZUICHENG_SERVER + 空格 + {"name": "<电脑名>", "port": 8000}

import 'dart:async';
import 'dart:convert';
import 'dart:io';

/// 找到的一台电脑。
class DiscoveredServer {
  const DiscoveredServer({
    required this.address,
    required this.port,
    required this.name,
    this.extraAddresses = const <String>[],
    this.cloudUrl,
  });

  /// 电脑在局域网里的 IPv4 地址（取值以「回包的来源地址」为准，最可靠）。
  final String address;
  final int port;

  /// 电脑名（显示给用户看，方便确认找到的是哪台机器）。
  final String name;

  /// 电脑自己报出来的**其它**地址（第二十九轮新增，通常是它的公网 IPv6）。
  ///
  /// 为什么要给：用户实测「同一网络下 IPv4 能连、手抄 IPv6 连不上」。
  /// 与其让用户去 start.bat 里抄，不如让电脑把它的每个地址都报一遍，
  /// App 挨个真连一下，谁能连上用谁。
  final List<String> extraAddresses;

  /// 电脑**当前对外的云端地址**（Cloudflare 隧道，形如 `wss://xxx.trycloudflare.com/ws`）。
  ///
  /// 2026-10-02 用户改用 Cloudflare Tunnel：快速隧道的网址**每次重启都会变**，
  /// 所以没法写死在 APK 里。改成「电脑把当前地址告诉手机」：
  /// 电脑开隧道时会写明在盘上（`cloud_url.txt`），探测应答里带过来，
  /// 手机存进设置，出门（连不上局域网）时自动切过去用 —— 用户一次都不用手填。
  final String? cloudUrl;

  /// 把主机拼成 WebSocket 地址（IPv6 要加方括号，否则一定连不上）。
  static String urlFor(String host, int port) {
    final String h = (host.contains(':') && !host.startsWith('[')) ? '[$host]' : host;
    return 'ws://$h:$port/ws';
  }

  String get wsUrl => urlFor(address, port);

  /// 所有可以试的地址：**电脑自报的排在前面**（服务端已经按「和探测方同网段」排过序），
  /// 回包来源地址只作兜底。
  ///
  /// 为什么来源地址不当第一（2026-10-02 实测）：电脑上装了 VMware 时，
  /// 广播回包会从虚拟网卡出去（实测来源地址是 192.168.x.x），
  /// 拿它去连必然失败 —— 这正是「自动寻找没用」的一个真实原因。
  ///
  List<String> get candidates {
    final List<String> out = <String>[];
    for (final String a in extraAddresses) {
      if (a.contains(':')) continue; // IPv6 不试（用户明确说不需要）
      final String u = urlFor(a, port);
      if (!out.contains(u)) out.add(u);
    }
    final String self = wsUrl;
    if (!self.contains('[') && !out.contains(self)) {
      out.add(self); // 兜底：至少还有回包来源地址可试
    }
    return out;
  }

  @override
  String toString() => '$name（$wsUrl）';
}

class ServerDiscovery {
  ServerDiscovery._();

  static const String probeMagic = 'ZUICHENG_PROBE';
  static const String replyMagic = 'ZUICHENG_SERVER';

  /// 默认端口（和服务端一致）。
  static const int defaultPort = 8000;

  /// 按网段扫出来的「电脑」拿不到电脑名（那是广播应答里才有的），用这个占位。
  static const String scannedName = '（按网段找到的电脑）';

  /// 解析一条应答。不是我们的应答、或格式不对，返回 null。
  ///
  /// [fromAddress] 是回包的来源 IP。第二十九轮起：来源地址只当**兜底**，
  /// 以正文里的 `addrs`（电脑自报、且按「和探测方同网段」排过序）为准 ——
  /// 实测回包可能从虚拟网卡出去，来源地址是手机永远到不了的那种。
  static DiscoveredServer? parseReply(String text, String fromAddress) {
    final String trimmed = text.trim();
    if (!trimmed.startsWith(replyMagic)) return null;
    final String body = trimmed.substring(replyMagic.length).trim();
    int port = defaultPort;
    String name = '';
    String? cloud;
    final List<String> extra = <String>[];
    if (body.isNotEmpty) {
      try {
        final Object? decoded = jsonDecode(body);
        if (decoded is Map) {
          final Object? p = decoded['port'];
          if (p is num && p > 0 && p <= 65535) port = p.toInt();
          name = decoded['name']?.toString() ?? '';
          // 电脑当前的云端地址（Cloudflare 隧道）；没有这个字段就是没开隧道
          final String c = decoded['cloud']?.toString().trim() ?? '';
          if (c.startsWith('ws://') || c.startsWith('wss://')) cloud = c;
          final Object? addrs = decoded['addrs'];
          if (addrs is List) {
            for (final Object? a in addrs) {
              final String s = a?.toString().trim() ?? '';
              // 空串/重复的不要；和来源地址相同的那条也留着 ——
              // 顺序是服务端按网段排好的，不能因为「等于来源」就把它挤到后面
              if (s.isNotEmpty && !extra.contains(s)) extra.add(s);
            }
          }
        }
      } catch (_) {
        // 正文坏了就当默认端口用，不影响「找到这台机器」这件事
      }
    }
    return DiscoveredServer(
      address: fromAddress,
      port: port,
      name: name,
      extraAddresses: extra,
      cloudUrl: cloud,
    );
  }

  /// 真连一下，看这个地址通不通（第二十九轮新增）。
  ///
  /// 为什么要有这一步：UDP 探测只证明「电脑在同一个网络里」，不证明
  /// 「这个地址能连上 WebSocket」——比如电脑有多张网卡、IPv6 被挡住、
  /// 地址过期了。所以 App 挨个试，只采用**真能连上**的那条。
  ///
  /// 只做 TCP 连接（登录令牌不参与），毫秒级返回，失败不抛异常。
  static Future<bool> probe(
    String wsUrl, {
    Duration timeout = const Duration(seconds: 2),
  }) async {
    final Uri? uri = Uri.tryParse(wsUrl);
    if (uri == null || uri.host.isEmpty || uri.port == 0) return false;
    try {
      final Socket s = await Socket.connect(uri.host, uri.port, timeout: timeout);
      s.destroy();
      return true;
    } catch (_) {
      return false;
    }
  }

  /// 本机所有 IPv4 网段的广播地址（比如手机当热点时是 `192.168.x.x`）。
  ///
  /// 2026-10-02 加：只发 `255.255.255.255` 不够 —— 手机自己做热点时，
  /// 它的默认路由是**流量**那张网卡，受限广播可能从流量网卡出去（或干脆发不出去），
  /// 热点里的电脑就永远收不到。往「本网段广播地址」发会走对那张网卡。
  static Future<List<String>> localBroadcasts() async {
    final Set<String> out = <String>{};
    try {
      final List<NetworkInterface> nis = await NetworkInterface.list(
        type: InternetAddressType.IPv4,
        includeLoopback: false,
        includeLinkLocal: false,
      );
      for (final NetworkInterface ni in nis) {
        for (final InternetAddress a in ni.addresses) {
          final List<String> p = a.address.split('.');
          if (p.length == 4) out.add('${p[0]}.${p[1]}.${p[2]}.255');
        }
      }
    } catch (_) {
      // 拿不到网卡列表就只发受限广播
    }
    return out.toList();
  }

  /// 挨个敲本网段的 `端口`（UDP 广播被挡时的兜底办法）。
  ///
  /// 为什么要有这条：广播能不能出去，取决于手机/路由器的实现（热点下尤其不可靠）。
  /// 但「同一网段里有一台电脑开着 8000 端口」这件事，直接连一下就能确定 ——
  /// 不依赖广播、不依赖任何第三方，最多两三秒（254 个地址、48 个并发）。
  static Future<List<String>> scanLocalSubnets({
    int port = defaultPort,
    Duration timeout = const Duration(milliseconds: 400),
    int concurrency = 48,
    String? preferredHost,
  }) async {
    final List<String> hits = <String>[];
    final Set<String> prefixes = <String>{};
    try {
      final List<NetworkInterface> nis = await NetworkInterface.list(
        type: InternetAddressType.IPv4,
        includeLoopback: false,
        includeLinkLocal: false,
      );
      for (final NetworkInterface ni in nis) {
        for (final InternetAddress a in ni.addresses) {
          final List<String> p = a.address.split('.');
          if (p.length == 4) prefixes.add('${p[0]}.${p[1]}.${p[2]}.');
        }
      }
    } catch (_) {
      return hits;
    }
    // 手机可能有好几张网卡（热点 + 流量 + VPN），挨个网段扫一遍太慢 ——
    // 先把「上次连过的地址」所在网段排到最前面（同一个网络里通常还在同一段）。
    final List<String> order = prefixes.toList();
    if (preferredHost != null && preferredHost.contains('.')) {
      final List<String> p = preferredHost.split('.');
      if (p.length == 4) {
        final String pref = '${p[0]}.${p[1]}.${p[2]}.';
        order.sort((String a, String b) {
          if (a == pref) return -1;
          if (b == pref) return 1;
          return a.compareTo(b);
        });
      }
    }
    for (final String prefix in order) {
      for (int start = 1; start <= 254; start += concurrency) {
        final int end = (start + concurrency - 1) > 254 ? 254 : (start + concurrency - 1);
        final List<Future<String?>> batch = <Future<String?>>[];
        for (int i = start; i <= end; i++) {
          final String host = '$prefix$i';
          batch.add(probe('ws://$host:$port/ws', timeout: timeout).then(
            (bool ok) => ok ? host : null,
          ));
        }
        final List<String?> results = await Future.wait(batch);
        for (final String? h in results) {
          if (h != null && !hits.contains(h)) hits.add(h);
        }
        // 找到一个就够（同一网段一般只有一台电脑开着这个端口），别再白扫下去
        if (hits.isNotEmpty) return hits;
      }
    }
    return hits;
  }

  /// 广播探测，最多等 [timeout]，返回去重后的候选（按地址去重）。
  ///
  /// [ports] 是要试的端口：默认试 8000 和 [knownPort]（App 里已保存的那个端口）。
  /// [fallbackScan] 为真时，广播一个都没回应就**按网段挨个敲端口**（2026-10-02 加，
  /// 用户「自动搜索服务器还是搜不到」）。
  static Future<List<DiscoveredServer>> find({
    Duration timeout = const Duration(seconds: 3),
    int? knownPort,
    String? knownHost,
    bool fallbackScan = true,
  }) async {
    final Set<int> ports = <int>{defaultPort, ?knownPort};
    final Map<String, DiscoveredServer> found = <String, DiscoveredServer>{};
    RawDatagramSocket? sock;
    try {
      sock = await RawDatagramSocket.bind(InternetAddress.anyIPv4, 0);
      sock.broadcastEnabled = true;
      final List<int> probe = utf8.encode(probeMagic);
      // 受限广播 + 本网段广播，两种都发一遍（热点下前者可能出不去）
      final Set<String> targets = <String>{'255.255.255.255'};
      targets.addAll(await localBroadcasts());
      for (final int p in ports) {
        for (final String t in targets) {
          try {
            sock.send(probe, InternetAddress(t), p);
          } catch (_) {
            // 某个目标/端口发不出去不影响其它
          }
        }
      }

      final Completer<void> done = Completer<void>();
      late final StreamSubscription<RawSocketEvent> sub;
      final Timer timer = Timer(timeout, () {
        if (!done.isCompleted) done.complete();
      });
      sub = sock.listen((RawSocketEvent event) {
        if (event != RawSocketEvent.read) return;
        final Datagram? dg = sock?.receive();
        if (dg == null) return;
        final String text = utf8.decode(dg.data, allowMalformed: true);
        final DiscoveredServer? server =
            parseReply(text, dg.address.address);
        if (server != null) {
          found[server.address] = server;
          // 已经找到一个就够了，不用等满超时
          if (!done.isCompleted) done.complete();
        }
      });
      await done.future;
      await sub.cancel();
      timer.cancel();
    } catch (_) {
      // 广播被系统/网络挡住时安静失败，调用方还有兜底扫描
    } finally {
      sock?.close();
    }

    if (found.isEmpty && fallbackScan) {
      for (final int p in ports) {
        for (final String host in await scanLocalSubnets(port: p, preferredHost: knownHost)) {
          found['$host:$p'] = DiscoveredServer(
            address: host,
            port: p,
            name: scannedName,
          );
        }
      }
    }
    return found.values.toList();
  }
}
