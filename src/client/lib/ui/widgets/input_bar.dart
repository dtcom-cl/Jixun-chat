// 底部输入栏：文本输入、表情面板与扩展菜单。
//
// 依据（加号扩展菜单、表情面板、发送按钮）。
//
// 五个按钮的行为：
// - 发送：把输入框内容发给 AI。未连接时明确提示，不静默失败。
// - 语音：不自己录音，而是把焦点交给输入框并唤起系统输入法，
//         用户用输入法的语音键说话（用户明确要求“点击后弹出系统输入法”）。
// - 表情：展开/收起自制表情面板，20 个 SVG；点一下把 [笑哭] 文本插入输入框。
// - 加号：展开/收起“扩展菜单”（本次 PRD 第二次验证补齐 3.1.1）。
//
// 2026-09-25 第二次 PRD 验证改动：
// 原来加号是“点一下直接开系统选图”，这只是“选图按钮”，不是 PRD 要求的
// “加号扩展菜单”。现在改成 QQ 的行为：
//   · 点加号 -> 输入栏下方展开一块扩展面板（与键盘、表情面板互斥）；
//   · 面板是 4 列网格，每项 = 白色圆角图标块 + 下方文字；
//   · 当前 App 真实支持的功能入口是「相册」（把本地图片压缩后发给 AI），
//     以后加功能只需往 _plusActions 里追加一项，布局不用动。
// 与表情面板互斥：打开一个自动收起另一个。

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../../services/image_picker_service.dart';
import 'emoji_panel.dart';

/// 扩展菜单里的一项功能。
class _PlusAction {
  const _PlusAction({
    required this.icon,
    required this.label,
    required this.onTap,
  });

  final IconData icon;
  final String label;
  final Future<void> Function() onTap;
}

class InputBar extends StatefulWidget {
  const InputBar({
    super.key,
    required this.onSendText,
    required this.onSendImage,
    required this.canSend,
  });

  /// 发送文本（用户点发送或按回车）。
  final void Function(String text) onSendText;

  /// 发送图片。
  final void Function(PickedImage image) onSendImage;

  /// 当前是否已连上服务端。未连接时发送会被拦下并提示。
  final bool Function() canSend;

  @override
  State<InputBar> createState() => _InputBarState();
}

class _InputBarState extends State<InputBar> {
  final TextEditingController _controller = TextEditingController();
  final FocusNode _focus = FocusNode();
  bool _canSend = false;
  bool _showEmoji = false;
  bool _showPlus = false;
  bool _picking = false;

  @override
  void initState() {
    super.initState();
    _controller.addListener(_onTextChanged);
  }

  @override
  void dispose() {
    _controller.removeListener(_onTextChanged);
    _controller.dispose();
    _focus.dispose();
    super.dispose();
  }

  void _onTextChanged() {
    final bool can = _controller.text.trim().isNotEmpty;
    if (can != _canSend) setState(() => _canSend = can);
  }

  void _toast(String text) {
    if (!mounted) return;
    ScaffoldMessenger.of(context)
      ..hideCurrentSnackBar()
      ..showSnackBar(SnackBar(
        content: Text(text),
        duration: const Duration(seconds: 3),
      ));
  }

  // ---------------------------------------------------------- 发送

  void _send() {
    final String text = _controller.text.trim();
    if (text.isEmpty) {
      _toast('先输入内容再发送');
      return;
    }
    if (!widget.canSend()) {
      _toast('未连接服务端，请到设置页确认服务器地址并点“保存并重连”');
      return;
    }
    widget.onSendText(text);
    _controller.clear();
    if (_showEmoji || _showPlus) {
      setState(() {
        _showEmoji = false;
        _showPlus = false;
      });
    }
  }

  // ---------------------------------------------------------- 语音

  /// 点语音：把焦点交给输入框并唤起系统输入法，用户用输入法的语音键说话。
  Future<void> _openVoiceInput() async {
    _closePanels();
    _focus.requestFocus();
    await SystemChannels.textInput.invokeMethod<void>('TextInput.show');
  }

  // ---------------------------------------------------------- 面板

  void _closePanels() {
    if (_showEmoji || _showPlus) {
      setState(() {
        _showEmoji = false;
        _showPlus = false;
      });
    }
  }

  void _toggleEmoji() {
    setState(() {
      _showEmoji = !_showEmoji;
      if (_showEmoji) _showPlus = false; // 两个面板互斥
    });
    if (_showEmoji) {
      // 收起键盘，给表情面板腾地方
      _focus.unfocus();
    } else {
      _focus.requestFocus();
    }
  }

  /// 加号：展开/收起扩展菜单（PRD 3.1.1）。
  void _togglePlus() {
    setState(() {
      _showPlus = !_showPlus;
      if (_showPlus) _showEmoji = false;
    });
    if (_showPlus) {
      // 展开面板时收起键盘，否则面板会被键盘顶掉——QQ 也是这个行为。
      _focus.unfocus();
    } else {
      _focus.requestFocus();
    }
  }

  void _insertEmoji(String code) {
    final TextEditingValue value = _controller.value;
    final TextSelection sel = value.selection;
    final int start = sel.isValid ? sel.start : value.text.length;
    final int end = sel.isValid ? sel.end : value.text.length;
    final String next = value.text.replaceRange(start, end, code);
    _controller.value = TextEditingValue(
      text: next,
      selection: TextSelection.collapsed(offset: start + code.length),
    );
  }

  // ---------------------------------------------------------- 扩展菜单动作

  Future<void> _pickImage() async {
    if (_picking) return;
    setState(() {
      _showPlus = false;
      _picking = true;
    });

    if (!widget.canSend()) {
      _toast('未连接服务端，请先到设置页确认服务器地址并点“保存并重连”');
      setState(() => _picking = false);
      return;
    }

    try {
      final PickResult r = await ImagePickerService.pickFromGallery();
      if (!mounted) return;
      if (r.unsupported) {
        _toast('当前平台不支持选图（Android 真机/模拟器可用）');
        return;
      }
      if (!r.isOk) {
        if (r.error != null) _toast('选图失败：${r.error}');
        return; // 其余情况是用户取消，静默返回
      }
      final PickedImage img = r.image!;
      widget.onSendImage(img);
      _toast('已发送图片（${img.sizeKb.toStringAsFixed(0)} KB）');
    } catch (e) {
      _toast('选图失败：$e');
    } finally {
      if (mounted) setState(() => _picking = false);
    }
  }

  // ---------------------------------------------------------- UI

  @override
  Widget build(BuildContext context) {
    return Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        Container(
          decoration: const BoxDecoration(
            color: Color(0xFFF7F7F7),
            border: Border(top: BorderSide(color: Color(0xFFE0E0E0))),
          ),
          padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 6),
          child: SafeArea(
            top: false,
            child: Row(
              crossAxisAlignment: CrossAxisAlignment.end,
              children: [
                _iconButton(
                  icon: Icons.mic_none,
                  tooltip: '语音（唤起输入法）',
                  onPressed: _openVoiceInput,
                ),
                Expanded(
                  child: Container(
                    constraints:
                        const BoxConstraints(minHeight: 38, maxHeight: 110),
                    padding: const EdgeInsets.symmetric(horizontal: 10),
                    decoration: BoxDecoration(
                      color: Colors.white,
                      borderRadius: BorderRadius.circular(6),
                    ),
                    child: TextField(
                      controller: _controller,
                      focusNode: _focus,
                      maxLines: null,
                      textInputAction: TextInputAction.send,
                      onSubmitted: (_) => _send(),
                      onTap: _closePanels, // 点输入框回到正常打字状态
                      decoration: const InputDecoration(
                        border: InputBorder.none,
                        isDense: true,
                        contentPadding: EdgeInsets.symmetric(vertical: 10),
                        hintText: '',
                      ),
                    ),
                  ),
                ),
                _iconButton(
                  icon: Icons.emoji_emotions_outlined,
                  tooltip: '表情',
                  active: _showEmoji,
                  onPressed: _toggleEmoji,
                ),
                _iconButton(
                  icon: Icons.add_circle_outline,
                  tooltip: '更多功能',
                  active: _showPlus,
                  onPressed: _togglePlus,
                ),
                const SizedBox(width: 2),
                ElevatedButton(
                  onPressed: _send,
                  style: ElevatedButton.styleFrom(
                    backgroundColor: const Color(0xFF0099FF),
                    foregroundColor: Colors.white,
                    disabledBackgroundColor: const Color(0xFFBBDDFF),
                    disabledForegroundColor: Colors.white,
                    elevation: 0,
                    padding: const EdgeInsets.symmetric(
                        horizontal: 16, vertical: 10),
                    shape: RoundedRectangleBorder(
                      borderRadius: BorderRadius.circular(6),
                    ),
                  ),
                  child: const Text('发送'),
                ),
              ],
            ),
          ),
        ),
        if (_showEmoji) EmojiPanel(onPick: _insertEmoji),
        if (_showPlus) _plusPanel(),
      ],
    );
  }

  /// QQ 式扩展菜单（PRD 3.1.1）：4 列网格，每项是白色圆角图标块 + 下方文字。
  Widget _plusPanel() {
    final List<_PlusAction> actions = <_PlusAction>[
      _PlusAction(
        icon: Icons.photo_library_outlined,
        label: '相册',
        onTap: _pickImage,
      ),
    ];

    return Container(
      color: const Color(0xFFF0F0F0),
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 14),
      child: SizedBox(
        height: 84,
        child: ListView.separated(
          scrollDirection: Axis.horizontal,
          itemCount: actions.length,
          separatorBuilder: (_, _) => const SizedBox(width: 16),
          itemBuilder: (BuildContext context, int i) {
            final _PlusAction a = actions[i];
            return GestureDetector(
              onTap: () => a.onTap(),
              behavior: HitTestBehavior.opaque,
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: <Widget>[
                  Container(
                    width: 52,
                    height: 52,
                    decoration: BoxDecoration(
                      color: Colors.white,
                      borderRadius: BorderRadius.circular(14),
                    ),
                    child: Icon(a.icon,
                        size: 26, color: const Color(0xFF555555)),
                  ),
                  const SizedBox(height: 6),
                  Text(
                    a.label,
                    style: const TextStyle(
                        fontSize: 11, color: Color(0xFF666666)),
                  ),
                ],
              ),
            );
          },
        ),
      ),
    );
  }

  Widget _iconButton({
    required IconData icon,
    required String tooltip,
    required VoidCallback onPressed,
    bool active = false,
  }) {
    return IconButton(
      onPressed: onPressed,
      tooltip: tooltip,
      icon: Icon(
        icon,
        color: active ? const Color(0xFF0099FF) : const Color(0xFF666666),
      ),
    );
  }
}