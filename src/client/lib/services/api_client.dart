// HTTP 接口客户端（PRD 3.4.1 群聊创建 / 3.1.6 会话列表与未读）。
//
// 为什么需要它：
// 早期只有单一固定会话，客户端依靠 WebSocket 即可运行；
// 群聊功能需要在设置里创建会话，并在消息列表中展示多个会话，
// 这些是 REST 接口，走 WebSocket 不合适。
//
// 为什么不引入 http 包：
// dart:io 自带的 HttpClient 已经够用，少一个依赖就少一份构建风险，
// 与 local_store.dart 不用 shared_preferences 的理由一致（文档未写时选更少依赖）。
//
// 地址换算：设置页里用户填的是 WebSocket 地址（ws://host:port/ws），
// 这里把 scheme 换成 http、丢掉 /ws 路径，得到 REST 根地址。

import 'dart:async';
import 'dart:convert';
import 'dart:io';

import '../config/app_config.dart';

/// 接口调用失败。message 直接拿去给用户看，不出现英文堆栈。
class ApiException implements Exception {
  ApiException(this.message, {this.statusCode});

  final String message;
  final int? statusCode;

  @override
  String toString() => message;
}

class ApiClient {
  ApiClient._();

  static final ApiClient instance = ApiClient._();

  /// 普通接口的超时（连接 / 等响应头 / 读响应体三处共用）。
  static const Duration _timeout = Duration(seconds: 10);

  /// 会**同步调用大模型**的接口单独给一个长超时。
  ///
  /// 2026-09-29 第十二轮修的 bug（用户：「人格微调的时候客户端总是会提示请求超时
  /// 请检查服务端，但是电脑端过了一会儿之后提示人格蒸馏成功、人格档案已保存」）：
  ///
  /// 人格蒸馏（`POST /api/ai-profiles/{id}/personality`）会在服务端**同步**调一次
  /// 大模型，实测几十秒才返回；而客户端这里原来只有一个写死的 10 秒。
  /// 于是必然超时 —— 但服务端不会因为你挂断就停下，它继续跑完、把人格存进库，
  /// 日志里照旧打印「蒸馏成功」。**两边其实都对，只是客户端先放弃了。**
  ///
  /// 后果不只是弹个错：超时抛异常后编辑框不会回填新人格，里面还是**旧的那份 JSON**，
  /// 用户以为失败、再点一次「保存」，就会把刚蒸馏好的新人格覆盖回旧的。
  static const Duration _longTimeout = Duration(minutes: 3);

  /// 由设置页里的 WS 地址换算出 REST 根地址；地址非法时返回空串。
  ///
  static String httpBase() {
    final String ws =
        AppConfig.normalizeServerUrl(AppConfig.instance.serverUrl);
    final Uri? uri = Uri.tryParse(ws);
    if (uri == null || uri.host.isEmpty) return '';
    final bool secure = uri.scheme == 'wss' || uri.scheme == 'https';
    final String scheme = secure ? 'https' : 'http';
    // Dart 的 uri.host 对 IPv6 会把方括号去掉，拼回去。
    final String host =
        uri.host.contains(':') ? '[${uri.host}]' : uri.host;
    final int port = uri.hasPort ? uri.port : (secure ? 443 : 8000);
    final bool defaultPort = (secure && port == 443) || (!secure && port == 80);
    return defaultPort ? '$scheme://$host' : '$scheme://$host:$port';
  }

  /// 令牌失效时的回调（Session 会把它接到「清凭证 -> 回登录页」）。
  static void Function()? onUnauthorized;

  Future<dynamic> _request(
    String method,
    String path, {
    Map<String, dynamic>? body,
    Duration? timeout,
  }) async {
    final String base = httpBase();
    if (base.isEmpty) {
      throw ApiException('服务器地址还没填好，请到设置页填写后重试');
    }

    // 默认 10 秒；同步调大模型的接口传 _longTimeout（见它的注释）。
    final Duration limit = timeout ?? _timeout;
    final HttpClient client = HttpClient()..connectionTimeout = limit;
    try {
      final Uri uri = Uri.parse('$base$path');
      final HttpClientRequest req =
          await client.openUrl(method, uri).timeout(limit);
      req.headers.set(HttpHeaders.acceptHeader, 'application/json');
      // 第六轮：所有 /api/* 都要求身份，令牌走 Authorization 头。
      // 没登录时（注册/登录接口本身）不带这个头，服务端也不要求。
      final String token = AppConfig.instance.token;
      if (token.isNotEmpty) {
        req.headers.set(HttpHeaders.authorizationHeader, 'Bearer $token');
      }
      if (body != null) {
        req.headers.contentType = ContentType.json;
        req.add(utf8.encode(jsonEncode(body)));
      }
      final HttpClientResponse res = await req.close().timeout(limit);
      final String text = await res
          .transform(utf8.decoder)
          .join()
          .timeout(limit);
      dynamic decoded;
      try {
        decoded = text.isEmpty ? null : jsonDecode(text);
      } catch (_) {
        decoded = null;
      }
      if (res.statusCode >= 200 && res.statusCode < 300) {
        return decoded;
      }
      // 401 = 令牌失效（被别处登录挤下线 / 主动退出 / 服务端换密钥）。
      // 必须让上层清掉本地凭证回登录页，否则用户会一直看着空列表。
      if (res.statusCode == 401) onUnauthorized?.call();
      throw ApiException(
        _detailOf(decoded) ?? '服务器返回 ${res.statusCode}',
        statusCode: res.statusCode,
      );
    } on ApiException {
      rethrow;
    } on TimeoutException {
      throw ApiException('请求超时，检查电脑上的服务端是否在运行');
    } on SocketException catch (e) {
      throw ApiException('连不上服务端（$base）：${e.osError?.message ?? e.message}');
    } catch (e) {
      throw ApiException('请求失败：$e');
    } finally {
      client.close(force: true);
    }
  }

  // ------------------------------------------------------------ 账号（第六轮）

  /// 注册：头像 + 昵称 + 密码。
  ///
  /// [avatar] 是 Base64（可带 data URL 前缀），为空表示不传头像。
  /// 服务端满员时抛的 ApiException.message 就是「服务器已满」，直接弹给用户。
  Future<Map<String, dynamic>> register({
    required String nickname,
    required String password,
    String avatar = '',
    String avatarExt = 'jpg',
  }) async {
    final dynamic data = await _request(
      'POST',
      '/api/auth/register',
      body: <String, dynamic>{
        'nickname': nickname,
        'password': password,
        'avatar': avatar,
        'avatar_ext': avatarExt,
      },
    );
    return data is Map<String, dynamic> ? data : <String, dynamic>{};
  }

  /// 登录：账号 ID + 密码。
  ///
  /// 密码错一次服务端要 3 秒才能再试，此时返回 429，[ApiException.message]
  /// 里带着剩余秒数，登录页会拿它做倒计时。
  Future<Map<String, dynamic>> login({
    required String accountId,
    required String password,
  }) async {
    final dynamic data = await _request(
      'POST',
      '/api/auth/login',
      body: <String, dynamic>{'account_id': accountId, 'password': password},
    );
    return data is Map<String, dynamic> ? data : <String, dynamic>{};
  }

  /// 退出登录：服务端忘掉令牌。
  Future<void> logout() async {
    try {
      await _request('POST', '/api/auth/logout');
    } on ApiException {
      // 服务端连不上也要让用户能退出：本地清干净就够了。
    }
  }

  /// 当前账号资料 + 全部自定义项。
  Future<Map<String, dynamic>> me() async {
    final dynamic data = await _request('GET', '/api/auth/me');
    final dynamic a = data is Map ? data['account'] : null;
    return a is Map<String, dynamic> ? a : <String, dynamic>{};
  }

  /// 改账号资料（昵称 / 头像 / 软件名称 / 软件图标 / 桌面图标身份）。
  Future<Map<String, dynamic>> updateMe({
    String? nickname,
    String? avatar,
    String? avatarExt,
    String? appName,
    String? iconId,
    int? launcherIndex,
  }) async {
    final Map<String, dynamic> body = <String, dynamic>{};
    if (nickname != null) body['nickname'] = nickname;
    if (avatar != null) {
      body['avatar'] = avatar;
      body['avatar_ext'] = avatarExt ?? 'jpg';
    }
    if (appName != null) body['app_name'] = appName;
    if (iconId != null) body['icon_id'] = iconId;
    if (launcherIndex != null) body['launcher_index'] = launcherIndex;
    final dynamic data = await _request('PATCH', '/api/auth/me', body: body);
    final dynamic a = data is Map ? data['account'] : null;
    return a is Map<String, dynamic> ? a : <String, dynamic>{};
  }

  /// 改密码。旧密码不对时服务端返回 401，且同样有 3 秒冷却。
  Future<void> changePassword({
    required String oldPassword,
    required String newPassword,
  }) async {
    await _request(
      'POST',
      '/api/auth/password',
      body: <String, dynamic>{
        'old_password': oldPassword,
        'new_password': newPassword,
      },
    );
  }

  /// 登录后一次性拉全量：账号资料 + 好友（含人格与设置）+ 会话。
  ///
  /// 用户要的是「换手机删软件重装，登录上还是之前的配置」，所以这条
  /// 尽量把首屏需要的东西一次给全。返回体里**不含 API KEY 明文**。
  Future<Map<String, dynamic>> sync() async {
    final dynamic data = await _request('GET', '/api/auth/sync');
    return data is Map<String, dynamic> ? data : <String, dynamic>{};
  }

  /// FastAPI 的错误体是 {"detail": "..."}，直接拿来展示。
  static String? _detailOf(dynamic decoded) {
    if (decoded is Map && decoded['detail'] != null) {
      return decoded['detail'].toString();
    }
    return null;
  }

  // ------------------------------------------------------------ 会话

  /// 会话列表（PRD 3.1.6 按最后一条消息时间排序，服务端已排好）。
  Future<List<Map<String, dynamic>>> listConversations() async {
    final dynamic data = await _request('GET', '/api/conversations');
    return _listOf(data, 'conversations');
  }

  /// 创建单聊会话（“加好友”第二步，用户 2026-09-27 要求）。
  ///
  /// 服务端对同一个 AI 幂等：已经有了就返回旧会话，`created` 为 false。
  Future<Map<String, dynamic>> createSingleConversation({
    required int aiProfileId,
  }) async {
    final dynamic data = await _request('POST', '/api/conversations', body: {
      'type': 'single',
      'participants': <int>[aiProfileId],
    });
    final dynamic conv = data is Map ? data['conversation'] : null;
    return conv is Map<String, dynamic> ? conv : <String, dynamic>{};
  }

  /// 新建 AI 档案（“加好友”第一步，用户 2026-09-27 要求）。
  ///
  /// 服务端 `/api/ai-profiles` 原本只有 GET，这里用到的是本批次新增的 POST。
  Future<Map<String, dynamic>> createAiProfile({required String name}) async {
    final dynamic data = await _request('POST', '/api/ai-profiles', body: {
      'name': name,
    });
    final dynamic ai = data is Map ? data['ai_profile'] : null;
    return ai is Map<String, dynamic> ? ai : <String, dynamic>{};
  }

  /// 创建群聊（PRD 3.4.1）。
  ///
  /// [backgroundRecords] 是上传的群聊聊天记录文档（上下文背景），
  /// 可以是用户直接粘贴的文本，也可以是结构化记录；为空表示不带背景。
  Future<Map<String, dynamic>> createGroup({
    required String title,
    required List<int> aiProfileIds,
    Object? backgroundRecords,
  }) async {
    final dynamic data = await _request('POST', '/api/conversations', body: {
      'type': 'group',
      'title': title,
      'participants': aiProfileIds,
      'background_records': ?backgroundRecords,
    });
    final dynamic conv = data is Map ? data['conversation'] : null;
    return conv is Map<String, dynamic> ? conv : <String, dynamic>{};
  }

  /// 历史消息（进入聊天页时拉取）。
  Future<List<Map<String, dynamic>>> listMessages(
    int conversationId, {
    int limit = 50,
  }) async {
    final dynamic data = await _request(
      'GET',
      '/api/conversations/$conversationId/messages?limit=$limit',
    );
    return _listOf(data, 'messages');
  }

  /// 未读清零（PRD 3.1.6）。失败不抛给界面，由调用方决定是否忽略。
  Future<int> markRead(int conversationId) async {
    final dynamic data =
        await _request('POST', '/api/conversations/$conversationId/read');
    if (data is Map && data['read_count'] is num) {
      return (data['read_count'] as num).toInt();
    }
    return 0;
  }

  /// 清空聊天记录（用户 2026-10-04 要求：人类好友与 AI 好友都有效）。
  ///
  /// 对 AI 好友来说，服务端会连它的记忆一起清（短期窗口 / 中期摘要 / 长期事实
  /// / 还没发出去的延迟消息），所以清完之后它**不再记得**之前聊过什么。
  /// 返回服务端删掉的条数，界面上用来提示。
  Future<Map<String, dynamic>> clearConversation(int conversationId) async {
    final dynamic data = await _request(
      'POST',
      '/api/conversations/$conversationId/clear',
    );
    return data is Map ? Map<String, dynamic>.from(data) : <String, dynamic>{};
  }

  // ------------------------------------------------------------ AI 档案

  /// 全部 AI 档案（创建群聊时勾选成员用）。
  Future<List<Map<String, dynamic>>> listAiProfiles() async {
    final dynamic data = await _request('GET', '/api/ai-profiles');
    return _listOf(data, 'ai_profiles');
  }

  /// 读取某个会话（某个 AI）的设置：头像、名称、API 四件套、人格数据。
  ///
  /// 用户 2026-09-27 要求：「每一个不同的聊天点进去后可以在右上角设置，
  /// 这个设置里面只能设置对方的头像、名称、API接口、APIKEY、人格对话数据」。
  /// API Key 只回后四位（has_key / key_tail），明文永不出服务端。
  Future<Map<String, dynamic>> getAiSettings(int aiProfileId) async {
    final dynamic data =
        await _request('GET', '/api/ai-profiles/$aiProfileId/settings');
    final dynamic s = data is Map ? data['settings'] : null;
    return s is Map<String, dynamic> ? s : <String, dynamic>{};
  }

  /// 保存会话设置。只传要改的字段；[apiKey] 传空串表示清空 Key，传 null 表示不改。
  /// [avatarBase64] 是头像图片本体（data URL），**不是**手机本地路径。
  ///
  /// 2026-09-28 第九轮修的 bug：以前传的是 `/data/user/0/.../ai_avatar.jpg`
  /// 这种手机私有目录路径，服务端存下来既没有这张图，客户端又拿它拼服务器
  /// 地址去请求，服务端日志里刷 404。现在传字节，服务端落盘回 `/uploads/`。
  Future<Map<String, dynamic>> updateAiSettings(
    int aiProfileId, {
    String? name,
    String? avatarUrl,
    String? avatarBase64,
    String? avatarExt,
    String? apiProvider,
    String? apiBaseUrl,
    String? apiModel,
    String? apiKey,
  }) async {
    final Map<String, dynamic> body = <String, dynamic>{};
    if (name != null) body['name'] = name;
    if (avatarUrl != null) body['avatar_url'] = avatarUrl;
    if (avatarBase64 != null) {
      body['avatar'] = avatarBase64;
      body['avatar_ext'] = avatarExt ?? 'jpg';
    }
    if (apiProvider != null) body['api_provider'] = apiProvider;
    if (apiBaseUrl != null) body['api_base_url'] = apiBaseUrl;
    if (apiModel != null) body['api_model'] = apiModel;
    if (apiKey != null) body['api_key'] = apiKey;
    final dynamic data =
        await _request('PATCH', '/api/ai-profiles/$aiProfileId', body: body);
    final dynamic s = data is Map ? data['settings'] : null;
    return s is Map<String, dynamic> ? s : <String, dynamic>{};
  }

  /// 删除一个 AI 好友（用户 2026-09-27 第六轮要求）。
  ///
  /// 入口在聊天设置页底部。服务端会把档案 / 人格 / 头像 / 单聊消息 /
  /// 延迟任务 / 记忆全部清掉；如果它在某个群里，只把它从群成员里移走，
  /// 群和别人说过的话都留着。
  ///
  /// 幂等：服务端对已经不存在的档案也返回 200（`deleted: false`），
  /// 所以用户手抖点两次不会看到报错。
  Future<Map<String, dynamic>> deleteAiProfile(int aiProfileId) async {
    final dynamic data =
        await _request('DELETE', '/api/ai-profiles/$aiProfileId');
    return data is Map<String, dynamic> ? data : <String, dynamic>{};
  }

  /// 直接覆盖某个人格档案（会话设置页里「人格对话数据」那个编辑框）。
  ///
  /// 与 [importPersonality] 的分工：
  /// - [importPersonality] 给的是原料（聊天记录），服务端蒸馏出人格；
  /// - 这里给的是成品（已经是一份人格 JSON），服务端只做字段补齐后落盘。
  /// 用户 2026-09-27 要求会话设置里能改「人格对话数据」，用的就是这条。
  Future<Map<String, dynamic>> importPersonalityRaw({
    required int aiProfileId,
    required String personalityData,
  }) async {
    final dynamic data = await _request(
      'PUT',
      '/api/ai-profiles/$aiProfileId/personality-data',
      body: <String, dynamic>{'personality_data': personalityData},
    );
    return data is Map<String, dynamic> ? data : <String, dynamic>{};
  }

  /// 导入人格档案（PRD 3.3.4 人格导入与性格生成）。
  ///
  /// [authorized] 必须为 true —— PRD 5.2 要求用户明确确认“已获得对方授权”，
  /// 服务端对 false / 缺省一律返回 403，这里把服务端的原话透出去即可。
  ///
  /// 2026-09-27 第七轮：**只传文本**（`chat_log`）。格式是
  ///
  ///     用户：兄弟你在干啥？
  ///     朋友：我在外面骑车呢，你要来不（3分钟后）
  ///
  /// 每行「说话人：内容」，行尾括号是相对上一条的间隔，空行分隔每一次发言。
  /// 服务端从这些括号里学首响延迟与段间隔（persona_text.parse_persona_text）。
  /// 旧的 records 数组入口服务端还留着给内部调用，客户端不再使用。
  Future<Map<String, dynamic>> importPersonality({
    required int aiProfileId,
    required String friendName,
    required String chatLog,
    required bool authorized,
  }) async {
    final dynamic data = await _request(
      'POST',
      '/api/ai-profiles/$aiProfileId/personality',
      body: <String, dynamic>{
        'friend_name': friendName,
        'chat_log': chatLog,
        'authorized': authorized,
      },
      // 这条会在服务端**同步**跑一次大模型蒸馏，几十秒很正常，
      // 必须用长超时。用默认的 10 秒必然误报「请求超时」，
      // 而服务端其实还在跑、跑完照旧把人格存进库（见 _longTimeout 的注释）。
      timeout: _longTimeout,
    );
    return data is Map<String, dynamic> ? data : <String, dynamic>{};
  }

  // ------------------------------------------------------------ 人类好友

  /// 我的人类好友列表（账号 ID、昵称、头像、在线状态）。
  Future<List<Map<String, dynamic>>> listHumanFriends() async {
    final dynamic data = await _request('GET', '/api/friends/human');
    return _listOf(data, 'friends');
  }

  /// 按 4 位账号加人类好友。
  ///
  /// 用户明确「不需要对方同意，输入账号即双向生效」，所以服务端直接建立
  /// 双向关系并给两边都建好会话。账号不存在时服务端返回 404「账号不存在」。
  Future<Map<String, dynamic>> addHumanFriend(String accountId) async {
    final dynamic data = await _request(
      'POST',
      '/api/friends/human',
      body: <String, dynamic>{'account_id': accountId},
    );
    return data is Map<String, dynamic> ? data : <String, dynamic>{};
  }

  /// 删人类好友。只要有一方删了，双向都没了（服务端语义）。
  Future<void> deleteHumanFriend(int peerUserId) async {
    await _request('DELETE', '/api/friends/human/$peerUserId');
  }

  static List<Map<String, dynamic>> _listOf(dynamic data, String key) {
    if (data is! Map) return <Map<String, dynamic>>[];
    final dynamic raw = data[key];
    if (raw is! List) return <Map<String, dynamic>>[];
    final List<Map<String, dynamic>> out = <Map<String, dynamic>>[];
    for (final dynamic item in raw) {
      if (item is Map<String, dynamic>) {
        out.add(item);
      } else if (item is Map) {
        out.add(Map<String, dynamic>.from(item));
      }
    }
    return out;
  }
}
