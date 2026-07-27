# Minimal SlimProto (Squeezebox) HELO listener on TCP :3483 - the pass/fail gate for path B2.
# Point a LARA at THIS host (Audio zone function + slim server IP), then run this and watch for HELO
# and which codecs it advertises (we need 'mp3'). This is a passive gate; for real playback use play_test.py.
# On Windows first allow inbound TCP 3483 (Admin PowerShell):
#   New-NetFirewallRule -DisplayName "LR3 SlimProto 3483" -Direction Inbound -Protocol TCP -LocalPort 3483 -Action Allow -Profile Any
param([int]$Port = 3483)

function L($m) { Write-Output ("[{0}] {1}" -f (Get-Date).ToString('HH:mm:ss.fff'), $m) }
function ReadExact($ns, [int]$n) {
  $b = [byte[]]::new($n); $o = 0
  while ($o -lt $n) { try { $r = $ns.Read($b, $o, $n - $o) } catch { return $null }; if ($r -le 0) { return $null }; $o += $r }
  return , $b
}
function Frame([string]$cmd, [byte[]]$payload) {
  $c = [System.Text.Encoding]::ASCII.GetBytes($cmd); $body = $c + $payload; $len = $body.Length
  return , ([byte[]]@([byte](($len -shr 8) -band 0xFF), [byte]($len -band 0xFF)) + $body)
}

$listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Any, $Port)
$listener.Start()
L "SlimProto listener UP on 0.0.0.0:$Port - waiting for LARA HELO (Ctrl+C to stop)"
while ($true) {
  $client = $listener.AcceptTcpClient(); $peer = $client.Client.RemoteEndPoint.ToString()
  L "CONNECT from $peer"
  $ns = $client.GetStream(); $ns.ReadTimeout = 600000
  try {
    while ($true) {
      $hdr = ReadExact $ns 8; if ($null -eq $hdr) { L "  peer closed"; break }
      $op = [System.Text.Encoding]::ASCII.GetString($hdr, 0, 4)
      $len = ([int]$hdr[4] -shl 24) -bor ([int]$hdr[5] -shl 16) -bor ([int]$hdr[6] -shl 8) -bor [int]$hdr[7]
      $data = @(); if ($len -gt 0) { $data = ReadExact $ns $len; if ($null -eq $data) { L "  short read on $op"; break } }
      if ($op -eq 'HELO') {
        $mac = (($data[2..7]) | ForEach-Object { $_.ToString('x2') }) -join ':'
        $txt = (-join ($data | ForEach-Object { if ($_ -ge 32 -and $_ -lt 127) { [char]$_ } else { '.' } }))
        L "  HELO dev_id=$($data[0]) mac=$mac len=$len"
        L "  HELO txt: $txt"
        foreach ($codec in 'mp3', 'flc', 'pcm', 'aac', 'wma', 'ogg', 'alc') { if ($txt.ToLower().Contains($codec)) { L "  >> advertises codec: $codec" } }
        $vf = Frame 'vers' ([System.Text.Encoding]::ASCII.GetBytes('7.9')); try { $ns.Write($vf, 0, $vf.Length); $ns.Flush() } catch {}
      } elseif ($op -eq 'BYE!') { L "  BYE!"; break }
      else { L "  <- op=$op len=$len" }
    }
  } catch { L ("  ERR: " + $_.Exception.Message) } finally { try { $client.Close() } catch {}; L "DISCONNECT $peer" }
}
