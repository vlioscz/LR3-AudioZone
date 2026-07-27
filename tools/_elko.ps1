# Shared ELKO LARA protocol helpers for the PowerShell dev tools.
# Loads the 1024-byte XOR mask straight from the add-on's elkoproto.py (single source of truth),
# and mirrors its code/decode + builders/parsers. Dot-source this from the other tools:
#     . "$PSScriptRoot\_elko.ps1"
# NOTE: cast bytes to [int] before -shl (a [byte] -shl 8 truncates to 0 in PowerShell).

$script:ELKO_PY = Join-Path $PSScriptRoot "..\lr3_audiozone\lr3ctl\elkoproto.py"
if (-not (Test-Path $script:ELKO_PY)) { throw "elkoproto.py not found at $script:ELKO_PY" }
$__src = Get-Content -Raw -LiteralPath $script:ELKO_PY
$__m = [regex]::Match($__src, 'b64decode\((?<b>[\s\S]*?)\)')
$MASK = [Convert]::FromBase64String(($__m.Groups['b'].Value -replace '["\s]', ''))
if ($MASK.Length -ne 1024) { throw "mask length $($MASK.Length), expected 1024" }

function Code-Packet([byte[]]$d, [int]$len) {
  $k = Get-Random -Minimum 0 -Maximum 700; $n = $k
  for ($i = 0; $i -lt $len; $i++) { if ($n -ge 1024) { $n = 0 }; $d[$i] = $d[$i] -bxor $MASK[$n]; $n++ }
  $d[$len] = ([int][math]::Floor($k / 256)) -band 0xFF; $d[$len + 1] = $k -band 0xFF; return , $d
}
function Recover-Key([byte[]]$c) {
  if ($c.Length -lt 4) { return $null }; $mg = @(0xFF, 0xFA, 0xFA, 0xFF)
  for ($k = 0; $k -lt 700; $k++) { $ok = $true
    for ($i = 0; $i -lt 4; $i++) { if (($c[$i] -bxor $MASK[($k + $i) % 1024]) -ne $mg[$i]) { $ok = $false; break } }
    if ($ok) { return $k } }
  return $null
}
function Decode-Packet([byte[]]$c) {
  $k = Recover-Key $c; if ($null -eq $k) { return $null }
  $o = [byte[]]::new($c.Length); $n = $k
  for ($i = 0; $i -lt $c.Length; $i++) { if ($n -ge 1024) { $n = 0 }; $o[$i] = $c[$i] -bxor $MASK[$n]; $n++ }
  return , $o
}
function Auth-Req([int]$flag, [int]$lenByte, [int]$subcmd, [string]$u, [string]$p, [int]$total) {
  $a = [byte[]]::new($total); $a[0] = 0xFF; $a[1] = 0xFA; $a[2] = 0xFA; $a[3] = 0xFF
  $a[4] = Get-Random -Minimum 0 -Maximum 256; $a[5] = $flag; $a[6] = $lenByte; $a[7] = 0x81; $a[8] = 0xC0; $a[9] = $subcmd; $a[10] = 0x11
  $ub = [System.Text.Encoding]::UTF8.GetBytes($u); if ($ub.Length -gt 17) { $ub = $ub[0..16] }
  $pb = [System.Text.Encoding]::UTF8.GetBytes($p); if ($pb.Length -gt 17) { $pb = $pb[0..16] }
  [Array]::Copy($ub, 0, $a, 11, $ub.Length); [Array]::Copy($pb, 0, $a, 28, $pb.Length); return , $a
}
function Build-TestPacket() {
  $a = [byte[]]::new(11); $a[0] = 0xFF; $a[1] = 0xFA; $a[2] = 0xFA; $a[3] = 0xFF
  $a[4] = Get-Random -Minimum 0 -Maximum 256; $a[5] = 7; $a[6] = 9; $a[7] = 0x80; $a[8] = 0; return (Code-Packet $a 9)
}
function Build-Probe([int]$seq = 0) {
  $a = [byte[]]::new(11); $a[0] = 0xFF; $a[1] = 0xFA; $a[2] = 0xFA; $a[3] = 0xFF
  $a[4] = Get-Random -Minimum 0 -Maximum 256; $a[5] = $seq -band 0xFF; $a[6] = 9; $a[7] = 0x80; $a[8] = 2; return (Code-Packet $a 9)
}
function Build-Status([string]$u, [string]$p) { return (Code-Packet (Auth-Req 7 49 0 $u $p 49) 47) }
function Build-Stations([string]$u, [string]$p, [int]$page) { $map = @{0 = 6; 1 = 12; 2 = 13; 3 = 14 }; return (Code-Packet (Auth-Req 1 45 $map[$page] $u $p 47) 45) }

function Parse-TestReply([byte[]]$c) {
  $d = Decode-Packet $c; if ($null -eq $d -or $d.Length -lt 15) { return $null }
  if ($d[8] -ne 1 -or $d[9] -ne 0 -or $d[10] -ne 3) { return $null }
  return [ordered]@{ fw = (([int]$d[11] * 65536) + ([int]$d[12] * 256) + [int]$d[13]); hw = $d[14] }   # [int] before shift!
}
function Parse-StatusReply([byte[]]$c) {
  $d = Decode-Packet $c; if ($null -eq $d -or $d.Length -lt 16) { return $null }
  if ($d[7] -ne 0 -or $d[8] -ne 0xC1 -or $d[9] -ne 1) { return $null }   # d[10] varies by fw (0 ref / 1 on 3.7.001)
  return [ordered]@{ source = $d[11]; station = $d[12]; volume = $d[13]; playing = ($d[15] -ne 0) }
}
function Parse-StationsReply([byte[]]$c) {
  $d = Decode-Packet $c; if ($null -eq $d -or $d.Length -lt 26) { return $null }
  if ($d[7] -ne 0 -or $d[8] -ne 0xC1 -or $d[9] -ne 7) { return $null }   # d[10] varies by fw
  $enc = [System.Text.Encoding]::GetEncoding(1250); $stride = 139; $names = @()
  for ($st = 0; $st -lt 10; $st++) { $base = 13 + $st * $stride; if ($base + 13 -gt $d.Length) { break }
    $nb = [System.Collections.Generic.List[byte]]::new(); for ($j = $base; $j -lt $base + 13; $j++) { if ($d[$j] -eq 0) { break }; $nb.Add($d[$j]) }
    $names += $enc.GetString($nb.ToArray()).Trim() }
  return [ordered]@{ page = $d[11]; count = $d[12]; names = $names }
}
function Parse-DiscoveryReply([byte[]]$c) {
  $d = Decode-Packet $c; if ($null -eq $d -or $d.Length -lt 42 -or $d.Length -ne ($d[6] + 2)) { return $null }
  if ((($d[9] -shl 8) -bor $d[10]) -ne 3) { return $null }   # DeviceID 3 = LARA
  $enc = [System.Text.Encoding]::GetEncoding(1250); $nb = [System.Collections.Generic.List[byte]]::new()
  foreach ($b in $d[19..35]) { if ($b -eq 0) { break }; $nb.Add($b) }
  return [ordered]@{
    ip = "$($d[15]).$($d[16]).$($d[17]).$($d[18])"; name = $enc.GetString($nb.ToArray()).Trim()
    mac = (($d[36..41] | ForEach-Object { $_.ToString('x2') }) -join ':')
    fw = (([int]$d[11] * 65536) + ([int]$d[12] * 256) + [int]$d[13]); hw = $d[14]
  }
}
function Tcp-Txn([string]$ip, [byte[]]$payload, [int]$timeoutMs = 2500) {
  $c = [System.Net.Sockets.TcpClient]::new()
  try {
    $iar = $c.BeginConnect($ip, 61695, $null, $null); if (-not $iar.AsyncWaitHandle.WaitOne($timeoutMs)) { $c.Close(); return $null }
    $c.EndConnect($iar); $ns = $c.GetStream(); $ns.ReadTimeout = $timeoutMs; $ns.Write($payload, 0, $payload.Length)
    $buf = [byte[]]::new(4096); $tot = 0; $sw = [System.Diagnostics.Stopwatch]::StartNew()
    while ($sw.ElapsedMilliseconds -lt 1800) {
      if ($ns.DataAvailable) { $n = $ns.Read($buf, $tot, $buf.Length - $tot); if ($n -le 0) { break }; $tot += $n }
      else { Start-Sleep -Milliseconds 60; if (-not $ns.DataAvailable -and $tot -gt 0) { break } }
    }
    $c.Close(); if ($tot -eq 0) { return $null }; return , $buf[0..($tot - 1)]
  } catch { try { $c.Close() } catch {}; return $null }
}
function Is-TcpOpen([string]$ip, [int]$port, [int]$ms = 1200) {
  $c = [System.Net.Sockets.TcpClient]::new()
  try { $iar = $c.BeginConnect($ip, $port, $null, $null); $ok = $iar.AsyncWaitHandle.WaitOne($ms)
    $r = ($ok -and $c.Connected); $c.Close(); return $r } catch { try { $c.Close() } catch {}; return $false }
}
