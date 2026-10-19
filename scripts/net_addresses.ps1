# 挑「手机该填哪一条地址」的公共逻辑（start_server.ps1 / show_addresses.ps1 / wan_local.ps1 共用）。
#
# 2026-10-02 第二十九轮，起因是用户的原话：
#   「就算输入 start 上显示的 IPV6 地址依然连不上，只有 IPV4 可以连」
#
# 为什么「印出来的地址」会连不上 —— 原来的挑法只有一句：
#     Get-NetIPAddress -AddressFamily IPv6 | ? { 不是 fe80 } | Sort SuffixOrigin | select -First 1
# 有两个坑：
#   1. 不过滤 AddressState。换网络之后（比如从家里 WiFi 换成手机热点），
#      旧前缀的地址不会马上消失，而是变成 **Deprecated（已弃用）** 挂在系统里，
#      照样会被挑出来印给用户 —— 用户抄了必然连不上，而且看起来「地址没错」。
#   2. 不过滤虚拟网卡。VMware / VirtualBox / WSL / Hyper-V / 蓝牙 都有自己的地址，
#      外人根本到不了。
#
# 现在的规则：
#   * 只认 AddressState = Preferred（把已弃用/试探中的全部排除）；
#   * 只在真实网卡上挑（按网卡名和描述排除虚拟适配器）；
#   * **优先当前上网那张网卡**（默认路由所在的那张）—— 手机要连的就是这张；
#   * IPv6 里 Link（EUI-64，固定）优先于 Random（临时地址，会轮换）。
#
# 本文件是 UTF-8 带 BOM 保存的（PowerShell 5.1 对无 BOM 的 UTF-8 中文脚本会按 GBK 解码）。
# 改完请用 Set-Content -Encoding UTF8 存回。

$ZzsVirtualAliasPattern = 'Loopback|vEthernet|VMware|VirtualBox|Hyper-V|WSL|Bluetooth|TAP-|TAP |Tailscale|ZeroTier|Npcap|Radmin|AnyConnect|Wintun|OpenVPN|VPN'

function Test-ZzsRealNic([int]$IfIndex) {
    if (-not $IfIndex) { return $false }
    $a = Get-NetAdapter -IfIndex $IfIndex -ErrorAction SilentlyContinue
    if (-not $a) { return $true }   # 查不到网卡信息就不拦（少数虚拟适配器查不到）
    if ($a.Name -match $ZzsVirtualAliasPattern) { return $false }
    if ($a.InterfaceDescription -match $ZzsVirtualAliasPattern) { return $false }
    return $true
}

function Get-ZzsActiveIfIndex {
    # 默认路由所在的网卡 = 当前真正在用的那张
    $r = Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue |
        Sort-Object RouteMetric, InterfaceMetric | Select-Object -First 1
    if ($r) { return [int]$r.InterfaceIndex }
    return 0
}

function Get-ZzsLanIp {
    # 局域网 IPv4（手机和电脑同一个 WiFi / 热点时用这条）
    $active = Get-ZzsActiveIfIndex
    $cands = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object {
            $_.AddressState -eq 'Preferred' -and
            $_.IPAddress -notlike '127.*' -and
            $_.IPAddress -notlike '169.254.*' -and
            $_.IPAddress -notlike '192.168.17.*' -and
            $_.IPAddress -notlike '192.168.73.*' -and
            ($_.IPAddress -like '192.168.*' -or $_.IPAddress -like '10.*' -or
             $_.IPAddress -like '172.1[6-9].*' -or $_.IPAddress -like '172.2[0-9].*' -or
             $_.IPAddress -like '172.3[01].*') -and
            (Test-ZzsRealNic $_.InterfaceIndex)
        }
    if (-not $cands) { return $null }
    $pick = $cands |
        Sort-Object @{Expression = { if ($_.InterfaceIndex -eq $active) { 0 } else { 1 } }}, InterfaceIndex |
        Select-Object -First 1
    return $pick.IPAddress
}

function Get-ZzsBestV6 {
    # 公网 IPv6（跨网络用这条；能不能通取决于运营商和光猫放不放行入站）
    $active = Get-ZzsActiveIfIndex
    $cands = Get-NetIPAddress -AddressFamily IPv6 -ErrorAction SilentlyContinue |
        Where-Object {
            $_.AddressState -eq 'Preferred' -and
            $_.IPAddress -ne '::1' -and
            $_.IPAddress -notlike 'fe80*' -and
            $_.SuffixOrigin -ne 'WellKnown' -and
            (Test-ZzsRealNic $_.InterfaceIndex)
        }
    if (-not $cands) { return $null }
    $pick = $cands |
        Sort-Object `
            @{Expression = { if ($_.InterfaceIndex -eq $active) { 0 } else { 1 } }}, `
            @{Expression = { if ($_.SuffixOrigin -eq 'Link') { 0 } elseif ($_.SuffixOrigin -eq 'Random') { 1 } else { 2 } }}, `
            InterfaceIndex |
        Select-Object -First 1
    return $pick.IPAddress
}

function Get-ZzsV6Detail {
    # 给 fix_wan.bat 那类脚本用：连 SuffixOrigin 一起返回，方便印给用户看
    $ip = Get-ZzsBestV6
    if (-not $ip) { return $null }
    return Get-NetIPAddress -AddressFamily IPv6 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -eq $ip } | Select-Object -First 1
}

function Get-ZzsPublicV4 {
    foreach ($u in 'https://ipinfo.io/ip', 'http://ip.3322.net') {
        try {
            $ip = (Invoke-WebRequest -Uri $u -TimeoutSec 6 -UseBasicParsing).Content.ToString().Trim()
            if ($ip) { return $ip }
        } catch { }
    }
    return $null
}
