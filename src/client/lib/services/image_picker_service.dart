// 选本地图片并转成可发送的 data URL（用户 2026-09-25 要求“+ 按钮选图片发给 AI”）。
//
// 为什么不用 image_picker 插件：
// 原生插件在 Windows 上构建需要系统“开发者模式”（符号链接权限），本机开不了，
// 会直接卡住整个构建。这里改成自己写的 MethodChannel（jixun/picker），
// 走系统 Intent.ACTION_GET_CONTENT 选图，并在原生侧用 BitmapFactory 降采样 + JPEG 压缩，
// 效果一样，而且不引入任何插件依赖。
//
// 发送格式沿用 已有的 content_type = image，content 存 data URL。

import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';

/// 选好的图片。
class PickedImage {
  const PickedImage({
    required this.dataUrl,
    required this.bytes,
    required this.sizeBytes,
  });

  /// data:image/jpeg;base64,... —— 直接作为 send 的 content。
  final String dataUrl;

  /// 原始字节，本地气泡直接用它渲染，不必再解码 base64。
  final Uint8List bytes;

  final int sizeBytes;

  double get sizeKb => sizeBytes / 1024;
}

/// 选图结果：成功 / 用户取消 / 当前平台不支持 / 失败。
class PickResult {
  const PickResult._(this.image, this.error, this.unsupported);

  const PickResult.ok(PickedImage image) : this._(image, null, false);
  const PickResult.cancelled() : this._(null, null, false);
  const PickResult.unsupported() : this._(null, null, true);
  const PickResult.failed(String message) : this._(null, message, false);

  final PickedImage? image;
  final String? error;
  final bool unsupported;

  bool get isOk => image != null;

  /// 用户点了取消（没选图，也不是错误）。设置页选头像时会用到。
  bool get cancelled => image == null && error == null && !unsupported;
}

class ImagePickerService {
  ImagePickerService._();

  static const MethodChannel _channel = MethodChannel('jixun/picker');

  /// 从系统相册选一张图并压缩。
  ///
  /// 图片压缩在原生侧完成（最长边 1024、JPEG 质量 70），
  /// 避免把几 MB 的原图塞进 WebSocket。
  static Future<PickResult> pickFromGallery() async {
    if (kIsWeb || defaultTargetPlatform != TargetPlatform.android) {
      return const PickResult.unsupported();
    }
    try {
      final Uint8List? bytes =
          await _channel.invokeMethod<Uint8List>('pickImage');
      if (bytes == null || bytes.isEmpty) return const PickResult.cancelled();
      final String dataUrl = 'data:image/jpeg;base64,${base64Encode(bytes)}';
      return PickResult.ok(PickedImage(
        dataUrl: dataUrl,
        bytes: bytes,
        sizeBytes: bytes.length,
      ));
    } on PlatformException catch (e) {
      return PickResult.failed(e.message ?? '选图失败');
    } on MissingPluginException {
      return const PickResult.unsupported();
    }
  }
}
