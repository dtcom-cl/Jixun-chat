# 极迅 - 服务端启动脚本（由根目录 start.bat 调用）

$ErrorActionPreference = 'Continue'
try { chcp 65001 | Out-Null } catch {}
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

try { [Console]::Title = '极迅 - 服务端' } catch {}

$root      = Split-Path -Parent $PSScriptRoot
$serverDir = Join-Path $root 'src\server'
$venvPy    = Join-Path $serverDir '.venv\Scripts\python.exe'

function Stop-Script([string]$msg) {
    Write-Host ''
    Write-Host "  [错误] $msg" -ForegroundColor Red
    Write-Host ''
    Read-Host '按回车键关闭'
    exit 1
}

Clear-Host
Write-Host '============================================================' -ForegroundColor Cyan
Write-Host '            极迅  -  服务端启动器' -ForegroundColor Cyan
Write-Host '============================================================' -ForegroundColor Cyan
Write-Host ''

if (-not (Test-Path (Join-Path $serverDir 'main.py'))) {
    Stop-Script "找不到 $serverDir\main.py。请把 start.bat 放在项目根目录再双击。"
}

Write-Host '[1/4] 检查 Python ...'
$python = $null
if (Get-Command python.exe -ErrorAction SilentlyContinue) { $python = 'python' }
elseif (Get-Command py.exe -ErrorAction SilentlyContinue)   { $python = 'py' }
if (-not $python) {
    Stop-Script '没找到 Python。请安装 Python 3.11 或更高版本，安装时勾选 Add to PATH。下载地址 https://www.python.org/downloads/'
}
$verText = (& $python --version 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0) { Stop-Script 'Python 无法运行，请重新安装并勾选 Add to PATH。' }
Write-Host "  OK: $verText" -ForegroundColor Green

Write-Host '[2/4] 准备运行环境 ...'
if (-not (Test-Path $venvPy)) {
    Write-Host '  第一次运行，正在创建 .venv，需要 10-30 秒 ...' -ForegroundColor Yellow
    & $python -m venv (Join-Path $serverDir '.venv')
    if (-not (Test-Path $venvPy)) { Stop-Script '创建虚拟环境失败。' }
    & $venvPy -m pip install --upgrade pip -q
}

& $venvPy -c 'import fastapi, uvicorn, websockets, pydantic' 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host '  正在安装依赖，需要 1-3 分钟 ...' -ForegroundColor Yellow
    & $venvPy -m pip install -r (Join-Path $serverDir 'requirements.txt') -q
    & $venvPy -c 'import fastapi, uvicorn, websockets, pydantic' 2>$null
    if ($LASTEXITCODE -ne 0) { Stop-Script '依赖安装失败。请检查网络后重新双击 start.bat。' }
}
Write-Host '  OK: 依赖就绪' -ForegroundColor Green

Write-Host '[3/4] 手机 App 里要填的服务器地址：'
Write-Host ''
$ips = @()
try {
    $ips = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop |
           Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' }
} catch {}
if ($ips.Count -gt 0) {
    foreach ($ip in ($ips | Sort-Object InterfaceAlias)) {
        Write-Host ('          ws://' + $ip.IPAddress + ':8000/ws      [' + $ip.InterfaceAlias + ']') -ForegroundColor Yellow
    }
} else {
    Write-Host '          （没检测到局域网网卡，请先连上 Wi-Fi 或网线）' -ForegroundColor DarkGray
}
Write-Host ''
Write-Host '          电脑本机自测:  ws://127.0.0.1:8000/ws' -ForegroundColor Gray
Write-Host '          安卓模拟器:    ws://10.0.2.2:8000/ws' -ForegroundColor Gray
Write-Host ''
Write-Host '          手机 App 里:  设置 -> 服务器地址，填上面局域网那一条，点“重连”' -ForegroundColor Gray
Write-Host ''
Write-Host '  提示：AI 回复走真实大模型，不再有任何写死的固定台词。' -ForegroundColor DarkGray
Write-Host '        第一次使用要先在 App 里点进某个聊天 -> 右上角设置，' -ForegroundColor DarkGray
Write-Host '        填「API 接口」和「APIKEY」。两种 Key 都支持，会自动识别：' -ForegroundColor DarkGray
Write-Host '          DeepSeek / OpenAI : sk-xxxxxxxx...' -ForegroundColor DarkGray
Write-Host '          智谱 GLM          : <32位id>.<secret>' -ForegroundColor DarkGray
Write-Host ''

Write-Host '[4/4] 启动服务端。关掉本窗口即停止。' -ForegroundColor Cyan
Write-Host '------------------------------------------------------------'
Write-Host ''

# 2026-10-01 第二十七轮（用户说「到底启动哪个」）：
# 把「手机该填什么」直接打在启动窗口里 —— 于是日常只需要这**一个**文件（start.bat），
# 不用再去记别的脚本叫什么。
#
# 2026-10-02 第二十九轮（用户「就算输入 start 上显示的 IPV6 地址依然连不上」）：
# 挑地址的逻辑抽到 scripts\net_addresses.ps1 —— 那边会排除**已弃用**的地址
# （换网络后旧地址会残留）和虚拟网卡，并优先当前上网的那张网卡。
$netHelper = Join-Path $PSScriptRoot 'net_addresses.ps1'
if (Test-Path $netHelper) { . $netHelper }
$lanIp = Get-ZzsLanIp

# 2026-10-02 第二十九轮补（用户：「局域网 IPV4 就够了，没必要再用 IPV6」）：
# 这里以前会推荐 IPv6 广域网地址，现在去掉了 —— 手机点「自动寻找服务器」就能自己填好，
# 手抄地址（尤其 IPv6）只会抄错。公网那条也不印了（你家是 CGNAT，本来就不通）。
Write-Host '================ 手机怎么连 ================' -ForegroundColor Green
if ($lanIp) {
    Write-Host "  手机 App → 设置 → 点「自动寻找服务器」（会自动填 ws://$lanIp`:8000/ws）" -ForegroundColor Green
    Write-Host "  手填也可以：ws://$lanIp`:8000/ws" -ForegroundColor Green
} else {
    Write-Host '  没找到局域网地址：先确认电脑连上了 WiFi 或手机热点' -ForegroundColor Yellow
}
Write-Host '  前提：手机和电脑在同一个 WiFi 或同一个手机热点下。' -ForegroundColor DarkGray
Write-Host '  出局域网（手机用流量）连不上是正常的：你家宽带是 CGNAT。' -ForegroundColor DarkGray
Write-Host '============================================' -ForegroundColor Green
Write-Host ''

Set-Location $serverDir
# 2026-09-27 第五轮：默认就是真实大模型管线（config.USE_LLM 默认 1）。
# 这里显式写死，一是让日志一眼看出当前模式，二是防止环境里残留的
# DSH_USE_LLM=0 把 AI 关掉（关掉之后没开 DSH_FIXED_REPLY 就完全不回复）。
$env:DSH_USE_LLM = '1'

# 2026-10-01 第二十五轮（用户要走 IPv6 广域网）：
# 用 src\server\run_server.py 启动 —— 它自己建一个 **IPV6_V6ONLY=0** 的 socket
# 再交给 uvicorn，所以是**真双栈**：同一个 WiFi 下的 IPv4 和任何网络下的 IPv6
# 同时都能连。
#
# 为什么不用 `uvicorn --host ::`：Windows 默认 IPV6_V6ONLY=1，那样只收 IPv6，
# 手机在同一个 WiFi 下用 IPv4 反而连不上（实测就是如此）。
# 为什么不用 uvicorn 的 fd= 参数：它在 Windows 上会走 socket.fromfd(AF_UNIX)，
# 这套 Python 没有 AF_UNIX，直接报错（也实测过）。
#
# 2026-10-02 用户「局域网 IPV4 就够了」：这里不再打印 IPv6 地址当推荐，
# 只说明监听是双栈（IPv4 照样收），日常用局域网 IPv4 那条就行。
& $venvPy run_server.py --port 8000

Write-Host ''
Write-Host '------------------------------------------------------------'
Write-Host '服务端已停止。'
Write-Host ''
Read-Host '按回车键关闭'