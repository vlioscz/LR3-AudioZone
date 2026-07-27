# Read-only path-A control smoke test against one LARA over TCP 61695.
# Sends the test packet (fw/hw), reads status (source/station/volume/playing) and the 40 presets.
# READ-ONLY - never writes presets/config. Never dumps raw packets (a config-read would leak passwords).
#   .\control_smoke.ps1 -Ip 10.0.0.98
param([Parameter(Mandatory = $true)][string]$Ip, [string]$User = "admin", [string]$Pass = "elkoep")
. "$PSScriptRoot\_elko.ps1"

Write-Output "=== TEST PACKET (unauth -> fw/hw) ==="
$r = Parse-TestReply (Tcp-Txn $Ip (Build-TestPacket))
if (-not $r) { Write-Output "  no valid ELKO test reply - wrong host / not a LARA?"; return }
Write-Output ("  LARA  fw={0}  hw={1}  fw_supported(35000..37999)={2}" -f $r.fw, $r.hw, ($r.fw -ge 35000 -and $r.fw -lt 38000))

Write-Output "=== STATUS (auth) ==="
$st = Parse-StatusReply (Tcp-Txn $Ip (Build-Status $User $Pass))
if ($st) {
  $src = @{1 = 'RADIO'; 3 = 'AUX'; 4 = 'DLNA' }[[int]$st.source]; if (-not $src) { $src = "?($($st.source))" }
  Write-Output ("  source={0}  station={1}  volume={2}  playing={3}" -f $src, $st.station, $st.volume, $st.playing)
} else { Write-Output "  status read failed (creds not ${User}/****? firmware?)" }

Write-Output "=== PRESETS (auth, read-only) ==="
$all = @()
for ($p = 0; $p -lt 4; $p++) {
  $sp = Parse-StationsReply (Tcp-Txn $Ip (Build-Stations $User $Pass $p))
  if ($sp) { $all += $sp.names } else { $all += (1..10 | ForEach-Object { '' }) }
}
$n = 0
for ($i = 0; $i -lt $all.Count; $i++) { if ($all[$i]) { Write-Output ("  [{0,2}] {1}" -f $i, $all[$i]); $n++ } }
Write-Output ("  ({0}/40 preset slots named)" -f $n)
