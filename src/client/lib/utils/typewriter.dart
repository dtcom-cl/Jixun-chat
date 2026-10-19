// 打字机引擎：控制多段消息的呈现节奏。
//
// 职责：把多段消息按顺序逐条呈现，而不是一次性全部出现。
// 依据：。
//
// 规则（PRD 3.1.5）：
// - 每条消息间隔 1-3 秒；
// - 长回复打字久，短回复打字快；
// - 偶尔出现“半句发出，停顿一下再补后半句”：由服务端把半句和后半句拆成两段下发，
//   客户端按段依次呈现即可自然形成该效果。

import 'dart:async';

/// 通用打字机：把排队的条目按 1-3 秒间隔依次交给 [onEmit]。
class Typewriter<T> {
  Typewriter({required this.onEmit, required this.textOf});

  /// 真正把条目展示到 UI 的回调。
  final Future<void> Function(T item) onEmit;

  /// 取出条目文本，用于计算打字间隔。
  final String Function(T item) textOf;

  final List<T> _queue = <T>[];
  bool _running = false;
  bool _stopped = false;

  int get pendingCount => _queue.length;
  bool get running => _running;

  /// 根据文本长度计算展示间隔，限制在 1-3 秒（PRD 3.1.5）。
  ///
  /// 短句（<=6 字）1 秒，长句逐步拉长，最多 3 秒。
  static Duration intervalForText(String text) {
    final len = text.length;
    if (len <= 6) return const Duration(milliseconds: 1000);
    final ms = 1000 + (len - 6) * 60;
    return Duration(milliseconds: ms.clamp(1000, 3000));
  }

  void enqueue(T item) {
    _queue.add(item);
    _start();
  }

  void enqueueAll(Iterable<T> items) {
    if (items.isEmpty) return;
    _queue.addAll(items);
    _start();
  }

  void clear() {
    _queue.clear();
  }

  void stop() {
    _stopped = true;
    _queue.clear();
  }

  void _start() {
    if (_running || _stopped) return;
    _running = true;
    unawaited(_pump());
  }

  Future<void> _pump() async {
    try {
      while (_queue.isNotEmpty && !_stopped) {
        final item = _queue.first;
        await Future<void>.delayed(intervalForText(textOf(item)));
        if (_stopped || _queue.isEmpty) break;
        _queue.removeAt(0);
        await onEmit(item);
      }
    } finally {
      _running = false;
    }
  }
}
