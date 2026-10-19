// 创建群聊页（PRD 3.4.1：用户可在设置中创建群聊、可上传特定群聊的聊天记录文档
// 作为上下文背景、多个 AI 进入同一个群聊）。
//
// - 群聊创建
// - 群聊管理器（服务端 POST /api/conversations）
//
// 交互：
//   1. 填群名（必填）；
//   2. 勾选成员 AI（至少一个）—— 列表来自服务端 /api/ai-profiles；
//   3. 背景文档（可选）两种填法：
//      · 从手机里选一个文本文件（.txt/.json/.md 等），原生侧读出来；
//      · 或者直接把聊天记录粘贴进输入框。
//   4. 点“创建” -> POST /api/conversations -> 回到消息列表。
//
// 背景文档的格式（服务端 group_chat.normalize_group_background 同时接受）：
//   - JSON 数组：[{"sender":"me","content":"...","timestamp":"..."}]，me=用户，ta=对方；
//   - 纯文本：原样保留，一行一句也行。
// 所以这里不强制校验格式，用户贴什么就传什么，服务端自己能识别。

import 'package:flutter/material.dart';

import '../services/api_client.dart';
import '../services/document_picker_service.dart';

class GroupCreatePage extends StatefulWidget {
  const GroupCreatePage({super.key});

  @override
  State<GroupCreatePage> createState() => _GroupCreatePageState();
}

class _GroupCreatePageState extends State<GroupCreatePage> {
  final TextEditingController _title = TextEditingController();
  final TextEditingController _background = TextEditingController();

  List<Map<String, dynamic>> _profiles = <Map<String, dynamic>>[];
  final Set<int> _selected = <int>{};

  bool _loading = true;
  bool _busy = false;
  String? _loadError;

  @override
  void initState() {
    super.initState();
    _loadProfiles();
  }

  @override
  void dispose() {
    _title.dispose();
    _background.dispose();
    super.dispose();
  }

  Future<void> _loadProfiles() async {
    setState(() {
      _loading = true;
      _loadError = null;
    });
    try {
      final List<Map<String, dynamic>> list =
          await ApiClient.instance.listAiProfiles();
      if (!mounted) return;
      setState(() {
        _profiles = list;
        // 只有一个 AI 时直接勾上，省一步点击
        if (list.length == 1) {
          final int? id = (list.first['id'] as num?)?.toInt();
          if (id != null) _selected.add(id);
        }
        _loading = false;
      });
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _loading = false;
        _loadError = e.toString();
      });
    }
  }

  void _toast(String text) {
    if (!mounted) return;
    ScaffoldMessenger.of(context)
      ..hideCurrentSnackBar()
      ..showSnackBar(SnackBar(content: Text(text)));
  }

  Future<void> _pickBackgroundFile() async {
    final DocumentPickResult r = await DocumentPickerService.pickTextFile();
    if (!mounted) return;
    if (r.unsupported) {
      _toast('当前平台不支持选文件，可以直接把聊天记录粘贴到下面的输入框');
      return;
    }
    if (r.cancelled) return;
    if (!r.isOk) {
      _toast('读取文件失败：${r.error}');
      return;
    }
    _background.text = r.text!;
    _toast('已读入 ${r.fileName ?? '文件'}（${r.text!.length} 字）');
  }

  Future<void> _create() async {
    final String title = _title.text.trim();
    if (title.isEmpty) {
      _toast('先给群聊起个名字');
      return;
    }
    if (_selected.isEmpty) {
      _toast('至少选一个 AI 进群');
      return;
    }

    setState(() => _busy = true);
    try {
      final String background = _background.text.trim();
      await ApiClient.instance.createGroup(
        title: title,
        aiProfileIds: _selected.toList()..sort(),
        backgroundRecords: background.isEmpty ? null : background,
      );
      if (!mounted) return;
      _toast('群聊“$title”已创建');
      Navigator.of(context).pop(true);
    } catch (e) {
      if (mounted) _toast('创建失败：$e');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: Colors.white,
      appBar: AppBar(
        backgroundColor: const Color(0xFFF7F7F7),
        elevation: 0.5,
        foregroundColor: Colors.black87,
        title: const Text('创建群聊', style: TextStyle(fontSize: 16)),
      ),
      body: _loading
          ? const Center(child: CircularProgressIndicator())
          : ListView(
              padding: const EdgeInsets.all(16),
              children: <Widget>[
                if (_loadError != null) _errorCard(_loadError!),
                _label('群名称'),
                TextField(
                  controller: _title,
                  decoration: _decoration('比如：老同学群'),
                ),
                const SizedBox(height: 20),
                _label('群成员（可多选）'),
                _memberList(),
                const SizedBox(height: 20),
                _label('群聊背景（可选）'),
                const Text(
                  '上传一段这个群以前的聊天记录，AI 会拿它当背景资料，'
                  '说话风格和关系更贴近原来的群。\n'
                  '支持两种填法：选一个文本文件，或直接粘贴聊天记录。',
                  style: TextStyle(fontSize: 12, color: Color(0xFF999999)),
                ),
                const SizedBox(height: 10),
                Row(
                  children: <Widget>[
                    OutlinedButton.icon(
                      onPressed: _busy ? null : _pickBackgroundFile,
                      icon: const Icon(Icons.upload_file, size: 18),
                      label: const Text('选择文件'),
                    ),
                    const SizedBox(width: 10),
                    OutlinedButton(
                      onPressed: _busy
                          ? null
                          : () => setState(() => _background.clear()),
                      child: const Text('清空'),
                    ),
                  ],
                ),
                const SizedBox(height: 8),
                TextField(
                  controller: _background,
                  maxLines: 8,
                  minLines: 5,
                  decoration: _decoration(
                    'me|2026-09-20 20:11\n老王：今天去哪\n我：随便\n'
                    '（或直接粘贴 JSON 记录）',
                  ),
                ),
                const SizedBox(height: 24),
                // 按钮缩小、居中定宽，不再横着霸占整个屏幕（用户 2026-09-27 要求）。
                Center(
                  child: SizedBox(
                    width: 170,
                    height: 42,
                    child: ElevatedButton(
                      onPressed: _busy ? null : _create,
                      style: ElevatedButton.styleFrom(
                        backgroundColor: const Color(0xFF0099FF),
                        foregroundColor: Colors.white,
                        padding: EdgeInsets.zero,
                      ),
                      child: Text(_busy ? '创建中…' : '创建群聊'),
                    ),
                  ),
                ),
                const SizedBox(height: 30),
              ],
            ),
    );
  }

  Widget _label(String text) => Padding(
        padding: const EdgeInsets.only(bottom: 8),
        child: Text(text,
            style: const TextStyle(fontSize: 14, color: Color(0xFF333333))),
      );

  InputDecoration _decoration(String hint) => InputDecoration(
        hintText: hint,
        hintStyle: const TextStyle(fontSize: 12, color: Color(0xFFBBBBBB)),
        isDense: true,
        contentPadding:
            const EdgeInsets.symmetric(horizontal: 12, vertical: 12),
        border: OutlineInputBorder(
          borderRadius: BorderRadius.circular(6),
          borderSide: const BorderSide(color: Color(0xFFDDDDDD)),
        ),
      );

  Widget _memberList() {
    if (_profiles.isEmpty) {
      return const Text('服务端还没有 AI 档案，请先到设置页确认服务器地址并重连',
          style: TextStyle(fontSize: 12, color: Color(0xFF999999)));
    }
    return Column(
      children: <Widget>[
        for (final Map<String, dynamic> p in _profiles)
          Builder(builder: (BuildContext context) {
            final int id = (p['id'] as num?)?.toInt() ?? 0;
            final String name = p['name']?.toString() ?? 'AI';
            return CheckboxListTile(
              value: _selected.contains(id),
              onChanged: (bool? v) => setState(() {
                if (v == true) {
                  _selected.add(id);
                } else {
                  _selected.remove(id);
                }
              }),
              dense: true,
              contentPadding: EdgeInsets.zero,
              controlAffinity: ListTileControlAffinity.leading,
              title: Text(name, style: const TextStyle(fontSize: 14)),
            );
          }),
      ],
    );
  }

  Widget _errorCard(String text) => Container(
        width: double.infinity,
        margin: const EdgeInsets.only(bottom: 16),
        padding: const EdgeInsets.all(12),
        decoration: BoxDecoration(
          color: const Color(0xFFFFECEC),
          borderRadius: BorderRadius.circular(8),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: <Widget>[
            const Text('读不到 AI 列表',
                style: TextStyle(
                    fontSize: 13,
                    fontWeight: FontWeight.w600,
                    color: Color(0xFF9B1C1C))),
            const SizedBox(height: 4),
            Text(text,
                style:
                    const TextStyle(fontSize: 12, color: Color(0xFF9B1C1C))),
            const SizedBox(height: 8),
            OutlinedButton(onPressed: _loadProfiles, child: const Text('重试')),
          ],
        ),
      );
}