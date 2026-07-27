# LARA discovery via UDP broadcast to 255.255.255.255:61695 (mirrors lr3ctl/discovery.py).
# On Windows the inbound reply is often dropped by the firewall -> if this finds nothing, use
# scan_tcp.ps1 instead (or allow inbound UDP 61695). On the real HA (Linux) this is the normal path.
. "$PSScriptRoot\_elko.ps1"

$udp = [System.Net.Sockets.UdpClient]::new(); $udp.EnableBroadcast = $true
try { $udp.Client.SetSocketOption([System.Net.Sockets.SocketOptionLevel]::Socket, [System.Net.Sockets.SocketOptionName]::ReuseAddress, $true) } catch {}
try { $udp.Client.Bind([System.Net.IPEndPoint]::new([System.Net.IPAddress]::Any, 61695)) } catch { $udp.Client.Bind([System.Net.IPEndPoint]::new([System.Net.IPAddress]::Any, 0)) }
$udp.Client.ReceiveTimeout = 400
Write-Output "UDP broadcast discovery on :61695 ..."
$found = @{}
for ($a = 0; $a -lt 3; $a++) {
  $probe = Build-Probe $a
  try { [void]$udp.Send($probe, $probe.Length, [System.Net.IPEndPoint]::new([System.Net.IPAddress]::Broadcast, 61695)) } catch {}
  $deadline = [DateTime]::UtcNow.AddSeconds(2)
  while ([DateTime]::UtcNow -lt $deadline) {
    $rep = [System.Net.IPEndPoint]::new([System.Net.IPAddress]::Any, 0)
    try { $data = $udp.Receive([ref]$rep) } catch { continue }
    $r = Parse-DiscoveryReply $data; if ($r) { if (-not $r.ip -or $r.ip -eq '0.0.0.0') { $r.ip = $rep.Address.ToString() }; $found[$r.mac] = $r }
  }
}
$udp.Close()
Write-Output ("Found {0} LARA(s):" -f $found.Count)
foreach ($k in $found.Keys) { $r = $found[$k]; Write-Output ("  name='{0}' ip={1} mac={2} fw={3} hw={4}" -f $r.name, $r.ip, $r.mac, $r.fw, $r.hw) }
if (-not $found.Count) { Write-Output "  (nothing - firewall likely dropped replies; try scan_tcp.ps1)" }
