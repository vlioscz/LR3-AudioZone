# Firewall-proof LARA discovery: TCP-scan port 61695 across a /24 and confirm each host with the
# unauthenticated ELKO test packet. Use this when UDP broadcast discovery finds nothing (Windows FW).
#   .\scan_tcp.ps1 -Subnet 10.0.0.
param([string]$Subnet = "10.0.0.", [string]$User = "admin", [string]$Pass = "elkoep")
. "$PSScriptRoot\_elko.ps1"

Write-Output "Scanning ${Subnet}1-254 tcp/61695 (batched)..."
$open = @(); $all = 1..254 | ForEach-Object { "$Subnet$_" }
for ($s = 0; $s -lt $all.Count; $s += 48) {
  $batch = $all[$s..([Math]::Min($s + 47, $all.Count - 1))]; $conns = @()
  foreach ($ip in $batch) { $c = [System.Net.Sockets.TcpClient]::new(); $null = $c.BeginConnect($ip, 61695, $null, $null); $conns += [pscustomobject]@{ ip = $ip; c = $c } }
  Start-Sleep -Milliseconds 1400
  foreach ($e in $conns) { if ($e.c.Connected) { $open += $e.ip }; try { $e.c.Close() } catch {} }
}
if (-not $open.Count) { Write-Output "No host has tcp/61695 open on ${Subnet}0/24."; return }
Write-Output ("61695 open: {0}" -f ($open -join ', '))
foreach ($ip in $open) {
  $r = Parse-TestReply (Tcp-Txn $ip (Build-TestPacket))
  if ($r) {
    $mac = (Get-NetNeighbor -IPAddress $ip -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty LinkLayerAddress)
    Write-Output ("  LARA @ {0}  fw={1} hw={2} mac={3} fw_supported(35000..37999)={4}" -f $ip, $r.fw, $r.hw, $mac, ($r.fw -ge 35000 -and $r.fw -lt 38000))
  } else { Write-Output ("  {0}: 61695 open but no valid ELKO test reply (some other service)" -f $ip) }
}
