// 选本地文本文件并读出内容（PRD 3.4.1：上传群聊聊天记录文档作为上下文背景）。
//
// 为什么和 image_picker_service 一样自己写 MethodChannel：
// 理由完全相同 —— 引入 file_picker 之类的原生插件会让 Windows 上的构建需要
// 系统“开发者模式”，本机开不了。这里走系统 Intent.ACTION_OPEN_CONTENT 选文件，
// 原生侧用 ContentResolver 直接读成 UTF-8 文本回传，不引入任何插件依赖。
//
// 只接受文本类文件：群聊背景是“聊天记录文档”，二进制文件读出来也是乱码，
// 不如在原生侧就按 mime/扩展名挡掉，给用户一句能看懂的话。

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';

/// 选文件的结果：成功 / 用户取消 / 当前平台不支持 / 失败。
class DocumentPickResult {
  const DocumentPickResult._(this.text, this.fileName, this.error, this.unsupported);

  const DocumentPickResult.ok(String text, String? fileName)
      : this._(text, fileName, null, false);
  const DocumentPickResult.cancelled() : this._(null, null, null, false);
  const DocumentPickResult.unsupported() : this._(null, null, null, true);
  const DocumentPickResult.failed(String message)
      : this._(null, null, message, false);

  /// 文件全文（UTF-8）。
  final String? text;

  /// 文件名，仅用于界面提示。
  final String? fileName;

  final String? error;
  final bool unsupported;

  bool get isOk => text != null;
  bool get cancelled => text == null && error == null && !unsupported;
}

class DocumentPickerService {
  DocumentPickerService._();

  static const MethodChannel _channel = MethodChannel('jixun/docpicker');

  /// 从系统文件管理器选一个文本文件并读出内容。
  static Future<DocumentPickResult> pickTextFile() async {
    if (kIsWeb || defaultTargetPlatform != TargetPlatform.android) {
      return const DocumentPickResult.unsupported();
    }
    try {
      final Map<dynamic, dynamic>? res =
          await _channel.invokeMethod<Map<dynamic, dynamic>>('pickTextFile');
      if (res == null) return const DocumentPickResult.cancelled();
      final String text = res['text']?.toString() ?? '';
      final String? name = res['name']?.toString();
      if (text.trim().isEmpty) {
        return const DocumentPickResult.failed('这个文件是空的');
      }
      return DocumentPickResult.ok(text, name);
    } on PlatformException catch (e) {
      return DocumentPickResult.failed(e.message ?? '读取文件失败');
    } on MissingPluginException {
      return const DocumentPickResult.unsupported();
    }
  }
}