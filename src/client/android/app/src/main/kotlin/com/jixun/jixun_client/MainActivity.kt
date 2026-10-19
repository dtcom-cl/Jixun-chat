package com.jixun.jixun_client

import android.app.Activity
import android.content.ComponentName
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodChannel
import java.io.ByteArrayOutputStream

/**
 * 两个原生通道：
 *
 * 1. jixun/launcher —— 桌面图标 / 桌面名称切换。
 *    Android 的 android:label 与 android:icon 是编译期资源，运行时改不成任意文本，
 *    所以 AndroidManifest.xml 里预置了 6 套 activity-alias（Alias1..Alias6），
 *    每套带自己的图标和名称；这里通过 PackageManager 启用其中一套、禁用其余各套。
 *
 * 2. jixun/picker —— 选本地图片发给 AI（“+”按钮）以及选头像（PRD 3.1.2）。
 *    自己实现而不引入 image_picker 插件：插件会要求 Windows 开发者模式才能构建，
 *    本机开不了；用系统 Intent 选图 + 自己压缩，效果一样且没有额外依赖。
 *
 * 3. jixun/docpicker —— 选一个文本文件读成 UTF-8 字符串（PRD 3.4.1
 *    群聊背景：上传该群的聊天记录文档）。同样不引入 file_picker 插件。
 */
class MainActivity : FlutterActivity() {

    private val launcherChannel = "jixun/launcher"
    private val pickerChannel = "jixun/picker"
    private val docPickerChannel = "jixun/docpicker"
    private val storageChannel = "jixun/storage"

    // 2026-10-01 第十八轮：用户要求「只有这两种能切换，其他的全部删掉」——
    // 现在只剩 Alias1(QQ) / Alias2(微信)，见 AndroidManifest.xml。
    private val aliasCount = 2
    private val pickImageRequest = 4001
    private val pickDocRequest = 4002

    /** 选图是异步的（等用户选完），先把 result 存起来，onActivityResult 里再回。 */
    private var pendingPickResult: MethodChannel.Result? = null

    /** 选文件同理。 */
    private var pendingDocResult: MethodChannel.Result? = null

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)

        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, launcherChannel)
            .setMethodCallHandler { call, result ->
                when (call.method) {
                    "setLauncher" -> {
                        val index = call.argument<Int>("index") ?: 1
                        result.success(applyLauncher(index))
                    }
                    // 第二十轮：先问「现在是哪一套」，已经是目标就不要再切（见 applyLauncher）
                    "currentLauncher" -> result.success(currentLauncher())
                    else -> result.notImplemented()
                }
            }

        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, pickerChannel)
            .setMethodCallHandler { call, result ->
                when (call.method) {
                    "pickImage" -> pickImage(result)
                    else -> result.notImplemented()
                }
            }

        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, docPickerChannel)
            .setMethodCallHandler { call, result ->
                when (call.method) {
                    "pickTextFile" -> pickTextFile(result)
                    else -> result.notImplemented()
                }
            }

        // 2026-10-01 第十七轮（用户报告「退出软件过一会儿不用又要重新登录」）：
        // 账号和令牌原来写在 **缓存目录** 里（Dart 的 Directory.systemTemp 在
        // Android 上落在 code_cache 下），系统清理缓存就等于「从来没登录过」。
        // 这里把真正该用的私有目录（filesDir，系统不会自动清）交给 Dart 侧。
        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, storageChannel)
            .setMethodCallHandler { call, result ->
                when (call.method) {
                    "filesDir" -> result.success(applicationContext.filesDir.absolutePath)
                    "cacheDir" -> result.success(applicationContext.cacheDir.absolutePath)
                    else -> result.notImplemented()
                }
            }
    }

    // ------------------------------------------------------------ 桌面图标

    /**
     * 当前启用的是第几套 alias（1 起）；一个都没启用返回 0。
     *
     * 2026-10-01 第二十轮（用户报告「切换图标时闪退、进去之后图标又变回企鹅」）：
     * 切换 alias 的启用状态在不少 ROM 上会**顺手把应用进程杀掉**
     * （launcher 入口被换掉，task 也跟着没了）。所以「启动时对账」必须能先问一句
     * 「现在已经是这一套了吗」——已经是就一个字节都不要动，避免每次启动都被杀。
     */
    private fun currentLauncher(): Int {
        val pm = applicationContext.packageManager
        for (i in 1..aliasCount) {
            val cn = ComponentName(applicationContext, "$packageName.Alias$i")
            val state = try {
                pm.getComponentEnabledSetting(cn)
            } catch (e: Exception) {
                PackageManager.COMPONENT_ENABLED_STATE_DEFAULT
            }
            if (state == PackageManager.COMPONENT_ENABLED_STATE_ENABLED) return i
            // DEFAULT = 跟着清单走，清单里只有 Alias1 是 enabled="true"
            if (state == PackageManager.COMPONENT_ENABLED_STATE_DEFAULT && i == 1) return i
        }
        return 0
    }

    /**
     * 启用第 index 套 alias（1 起），禁用其余各套。返回是否成功。
     *
     * 幂等：已经是这一套就**什么都不做**直接返回 true
     * （只改真正需要改的那几个，切不动的 ROM 少被杀几次）。
     */
    private fun applyLauncher(index: Int): Boolean {
        if (index < 1 || index > aliasCount) return false
        if (currentLauncher() == index) return true
        val pm = applicationContext.packageManager

        return try {
            for (i in 1..aliasCount) {
                val cn = ComponentName(applicationContext, "$packageName.Alias$i")
                val want = i == index
                val state = try {
                    pm.getComponentEnabledSetting(cn)
                } catch (e: Exception) {
                    PackageManager.COMPONENT_ENABLED_STATE_DEFAULT
                }
                val already = state == PackageManager.COMPONENT_ENABLED_STATE_ENABLED ||
                    (state == PackageManager.COMPONENT_ENABLED_STATE_DEFAULT && i == 1)
                if (already == want) continue // 已经是想要的状态，别动它
                val newState = if (want) {
                    PackageManager.COMPONENT_ENABLED_STATE_ENABLED
                } else {
                    PackageManager.COMPONENT_ENABLED_STATE_DISABLED
                }
                // DONT_KILL_APP：切图标不重启应用，避免用户正聊天时被踢出去
                // （部分 ROM 仍会杀进程，所以调用方必须先落盘再切，见 Dart 侧）
                pm.setComponentEnabledSetting(cn, newState, PackageManager.DONT_KILL_APP)
            }
            true
        } catch (e: Exception) {
            false
        }
    }

    // ------------------------------------------------------------ 选图

    private fun pickImage(result: MethodChannel.Result) {
        if (pendingPickResult != null) {
            result.error("busy", "正在选择图片，请稍候", null)
            return
        }
        pendingPickResult = result

        val intent = Intent(Intent.ACTION_GET_CONTENT).apply {
            type = "image/*"
            addCategory(Intent.CATEGORY_OPENABLE)
        }
        try {
            startActivityForResult(
                Intent.createChooser(intent, "选择图片"),
                pickImageRequest
            )
        } catch (e: Exception) {
            pendingPickResult = null
            result.error("no_picker", "系统里没有可用的图片选择器", e.message)
        }
    }

    // ------------------------------------------------------------ 选文本文件

    /**
     * 选一个文本文件（PRD 3.4.1 群聊背景文档）。
     *
     * type 用通配（而不是只限定纯文本）：
     * 很多聊天记录导出文件在系统里的 mime 是 application/json、
     * application/octet-stream 甚至空，写死只认纯文本会让用户根本选不中自己的文件。
     * 这里放宽，读到内容后由 Dart 侧判断是不是空的；真的是二进制会读出乱码，
     * 用户自己能看出来，比“选不了文件”体验好。
     *
     * 注意：本段注释里不能出现星号加斜杠的组合，否则 KDoc 会被提前闭合。
     * 2026-09-26：这个坑真实发生过 —— Gradle 报了一整屏 Kotlin 语法错误。
     */
    private fun pickTextFile(result: MethodChannel.Result) {
        if (pendingDocResult != null) {
            result.error("busy", "正在选择文件，请稍候", null)
            return
        }
        pendingDocResult = result

        val intent = Intent(Intent.ACTION_GET_CONTENT).apply {
            type = "*/*"
            addCategory(Intent.CATEGORY_OPENABLE)
        }
        try {
            startActivityForResult(
                Intent.createChooser(intent, "选择聊天记录文件"),
                pickDocRequest
            )
        } catch (e: Exception) {
            pendingDocResult = null
            result.error("no_picker", "系统里没有可用的文件选择器", e.message)
        }
    }

    /** 读取文本文件内容（UTF-8），顺带把文件名带回 Dart 侧做提示。 */
    private fun readTextFile(uri: android.net.Uri): Map<String, String>? {
        val text = contentResolver.openInputStream(uri)?.use { stream ->
            stream.bufferedReader(Charsets.UTF_8).readText()
        } ?: return null
        return mapOf("text" to text, "name" to (queryDisplayName(uri) ?: "聊天记录"))
    }

    private fun queryDisplayName(uri: android.net.Uri): String? {
        return try {
            contentResolver.query(uri, null, null, null, null)?.use { cursor ->
                val idx = cursor.getColumnIndex(android.provider.OpenableColumns.DISPLAY_NAME)
                if (idx >= 0 && cursor.moveToFirst()) cursor.getString(idx) else null
            }
        } catch (e: Exception) {
            null
        }
    }

    @Deprecated("startActivityForResult 仍然兼容所有 Android 版本，阶段1用它最稳")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        if (requestCode == pickDocRequest) {
            val result = pendingDocResult
            pendingDocResult = null
            if (result == null) {
                super.onActivityResult(requestCode, resultCode, data)
                return
            }
            val uri = data?.data
            if (resultCode != Activity.RESULT_OK || uri == null) {
                result.success(null) // 用户取消，不是错误
                return
            }
            try {
                val payload = readTextFile(uri)
                if (payload == null) {
                    result.error("read_failed", "这个文件读不出来，换一个试试", null)
                } else {
                    result.success(payload)
                }
            } catch (e: Exception) {
                result.error("read_failed", e.message ?: "读取文件失败", null)
            }
            return
        }
        if (requestCode == pickImageRequest) {
            val result = pendingPickResult
            pendingPickResult = null
            if (result == null) {
                super.onActivityResult(requestCode, resultCode, data)
                return
            }
            val uri = data?.data
            if (resultCode != Activity.RESULT_OK || uri == null) {
                result.success(null) // 用户取消，不是错误
                return
            }
            try {
                val bytes = readCompressJpeg(uri)
                if (bytes == null || bytes.isEmpty()) {
                    result.error("decode_failed", "这张图读不出来，换一张试试", null)
                } else {
                    result.success(bytes)
                }
            } catch (e: Exception) {
                result.error("pick_failed", e.message ?: "读取图片失败", null)
            }
            return
        }
        super.onActivityResult(requestCode, resultCode, data)
    }

    /**
     * 读取图片并按最长边 [maxSide] 降采样，再压成 JPEG。
     *
     * 两步走：先用 inJustDecodeBounds 只读尺寸算出采样率，
     * 再真正解码 —— 否则一张 4000x3000 的原图会直接吃掉几十 MB 内存。
     */
    private fun readCompressJpeg(uri: android.net.Uri): ByteArray? {
        val maxSide = 1024

        val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        contentResolver.openInputStream(uri)?.use { stream ->
            BitmapFactory.decodeStream(stream, null, bounds)
        }
        if (bounds.outWidth <= 0 || bounds.outHeight <= 0) return null

        var sample = 1
        while (bounds.outWidth / sample > maxSide * 2 ||
            bounds.outHeight / sample > maxSide * 2
        ) {
            sample *= 2
        }

        val opts = BitmapFactory.Options().apply { inSampleSize = sample }
        val decoded = contentResolver.openInputStream(uri)?.use { stream ->
            BitmapFactory.decodeStream(stream, null, opts)
        } ?: return null

        // 再按最长边精确缩一次，保证不会明显超过 1024
        val longest = maxOf(decoded.width, decoded.height)
        val scaled = if (longest > maxSide) {
            val ratio = maxSide.toFloat() / longest.toFloat()
            Bitmap.createScaledBitmap(
                decoded,
                (decoded.width * ratio).toInt().coerceAtLeast(1),
                (decoded.height * ratio).toInt().coerceAtLeast(1),
                true
            )
        } else {
            decoded
        }

        val out = ByteArrayOutputStream()
        scaled.compress(Bitmap.CompressFormat.JPEG, 70, out)
        if (scaled !== decoded) scaled.recycle()
        decoded.recycle()
        return out.toByteArray()
    }
}