# 极迅 - APK 打包脚本（由根目录 build_apk.bat 调用）
#
# 设计约束：
#   1. 工具链位置从环境变量解析：FLUTTER_ROOT、ANDROID_HOME、JAVA_HOME、
#      GRADLE_USER_HOME、PUB_CACHE。全部缺失时报出明确的设置提示。
#   2. Dart 的 AOT snapshotter 与 Gradle 在 Windows 上无法读取含中文的路径，
#      因此先把 src\client 同步到系统盘根目录下的纯 ASCII 暂存目录再构建，
#      完成后把 APK 拷回 dist\apk。暂存目录可随时删除，构建时自动重建。
#   3. Gradle 默认的 G1 GC 配多 worker 在内存较小的机器上可能触发
#      "Native memory allocation (malloc) failed"，这里改用 SerialGC
#      并限制 worker 数量，降低 native memory 占用。

param([string]$Batch = '')

$ErrorActionPreference = 'Continue'
try { chcp 65001 | Out-Null } catch {}
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
try { [Console]::Title = '极迅 - 打包 APK' } catch {}

$root      = Split-Path -Parent $PSScriptRoot
$clientDir = Join-Path $root 'src\client'
$distDir   = Join-Path $root 'dist\apk'

# ---- 工具链位置：优先环境变量 ---------------------------------------------
$flutterRoot = $env:FLUTTER_ROOT
if (-not $flutterRoot) {
    $fbCmd = Get-Command flutter.bat -ErrorAction SilentlyContinue
    if ($fbCmd) { $flutterRoot = Split-Path -Parent (Split-Path -Parent $fbCmd.Source) }
}
$flutterBin  = if ($flutterRoot) { Join-Path $flutterRoot 'bin' } else { $null }

$androidSdk  = $env:ANDROID_HOME
if (-not $androidSdk) { $androidSdk = $env:ANDROID_SDK_ROOT }
$jdkDir      = $env:JAVA_HOME

$gradleHome  = $env:GRADLE_USER_HOME
if (-not $gradleHome) { $gradleHome = Join-Path $env:USERPROFILE '.gradle' }
$pubCache    = $env:PUB_CACHE
if (-not $pubCache) { $pubCache = Join-Path $env:LOCALAPPDATA 'Pub\Cache' }

# 纯 ASCII 暂存目录：系统盘根目录，避免中文路径导致 AOT 读不到 app.dill
$stageDir    = Join-Path $env:SystemDrive 'jixun_build'
$stageTmp    = Join-Path $stageDir 'temp'
$stageClient = Join-Path $stageDir 'client'
$apkSrc      = Join-Path $stageClient 'build\app\outputs\flutter-apk\app-release.apk'

function Stop-Script([string]$msg) {
    Write-Host ''
    Write-Host "  [错误] $msg" -ForegroundColor Red
    Write-Host ''
    Read-Host '按回车键关闭'
    exit 1
}

Clear-Host
Write-Host '============================================================' -ForegroundColor Cyan
Write-Host '            极迅  -  打包 APK' -ForegroundColor Cyan
Write-Host '============================================================' -ForegroundColor Cyan
Write-Host ''

if (-not (Test-Path (Join-Path $clientDir 'pubspec.yaml'))) {
    Stop-Script "找不到 $clientDir\pubspec.yaml。请把 build_apk.bat 放在项目根目录再双击。"
}
if (-not $flutterBin -or -not (Test-Path (Join-Path $flutterBin 'flutter.bat'))) {
    Stop-Script '找不到 Flutter SDK。把 Flutter 的 bin 目录加入 PATH，或设置环境变量 FLUTTER_ROOT 指向 SDK 根目录。'
}
if (-not $androidSdk -or -not (Test-Path $androidSdk)) {
    Stop-Script '找不到 Android SDK。设置环境变量 ANDROID_HOME 指向 SDK 目录。'
}
if (-not $jdkDir -or -not (Test-Path (Join-Path $jdkDir 'bin\javac.exe'))) {
    Stop-Script '找不到 JDK。设置环境变量 JAVA_HOME 指向 JDK 目录。'
}

# 所有缓存 / 临时目录都指向 D 盘
foreach ($d in @($gradleHome, $pubCache, $stageTmp)) {
    if (-not (Test-Path $d)) { New-Item -ItemType Directory -Path $d -Force | Out-Null }
}

$env:Path             = "$flutterBin;$flutterBin\cache\dart-sdk\bin;$jdkDir\bin;$env:Path"
$env:ANDROID_HOME     = $androidSdk
$env:ANDROID_SDK_ROOT = $androidSdk
$env:JAVA_HOME        = $jdkDir
$env:GRADLE_USER_HOME = $gradleHome
$env:PUB_CACHE        = $pubCache
$env:TEMP             = $stageTmp
$env:TMP              = $stageTmp

if ([string]::IsNullOrWhiteSpace($Batch)) { $Batch = 'dev' }
$date    = Get-Date -Format 'yyyyMMdd'
$apkName = "jixun-$Batch-$date.apk"

Write-Host "  批次     : $Batch"
Write-Host "  产物     : $apkName"
Write-Host "  JDK      : $jdkDir"
Write-Host "  Gradle   : $gradleHome"
Write-Host "  Pub 缓存 : $pubCache"
Write-Host ''

Write-Host "[1/4] 同步源码到 ASCII 暂存路径 $stageClient ..."
if (-not (Test-Path $stageDir)) { New-Item -ItemType Directory -Path $stageDir -Force | Out-Null }
robocopy $clientDir $stageClient /MIR /XD build .dart_tool .gradle .idea .kotlin /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { Stop-Script "复制源码失败。请确认 D 盘可写、$stageDir 没被占用。" }
Write-Host '  OK' -ForegroundColor Green

Write-Host '[2/4] 拉取依赖 ...'
Set-Location $stageClient
& flutter pub get
if ($LASTEXITCODE -ne 0) { Stop-Script 'flutter pub get 失败，请检查网络。' }

Write-Host ''
Write-Host '[3/4] 构建 release APK。第一次要 5-15 分钟，之后 1-2 分钟 ...' -ForegroundColor Yellow
Write-Host ''
& flutter build apk --release --target-platform android-arm64
if ($LASTEXITCODE -ne 0) {
    Stop-Script '构建失败，请查看上面的输出。常见原因：连不上 maven.google.com / dl.google.com（Gradle 要下依赖）。'
}
if (-not (Test-Path $apkSrc)) { Stop-Script "构建结束但没找到 $apkSrc" }

Write-Host ''
Write-Host '[4/4] 复制到 dist\apk ...'
if (-not (Test-Path $distDir)) { New-Item -ItemType Directory -Path $distDir -Force | Out-Null }
Copy-Item $apkSrc (Join-Path $distDir $apkName) -Force
Copy-Item $apkSrc (Join-Path $distDir 'app-release.apk') -Force

$sizeMB = [math]::Round((Get-Item $apkSrc).Length / 1MB, 1)
Write-Host ''
Write-Host '============================================================' -ForegroundColor Cyan
Write-Host '  打包完成' -ForegroundColor Green
Write-Host '============================================================' -ForegroundColor Cyan
Write-Host ''
Write-Host "  本批次 APK : dist\apk\$apkName  ($sizeMB MB)"
Write-Host '  最新一份   : dist\apk\app-release.apk'
Write-Host ''
Write-Host '  装机后到 App 设置页把服务器地址改成:'
Write-Host '      ws://电脑局域网IP:8000/ws' -ForegroundColor Yellow
Write-Host '  （双击根目录 start.bat，窗口里会列出这个地址）'
Write-Host ''
Read-Host '按回车键关闭'