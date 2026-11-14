# XD KVM clipboard agent cho Windows (W105D kvmweb phat file nay, __BASES__ duoc thay khi tai).
# Dong bo clipboard van ban 2 chieu voi W105D qua HTTP: POST /clip/agent moi ~0,6 s.
# Go: bam "Go agent" trong trang KVM, hoac xoa %LOCALAPPDATA%\xdclip va shortcut "XD KVM Clipboard" trong Startup.
Add-Type -AssemblyName System.Windows.Forms
$Bases = @(__BASES__)
$Dir = Join-Path $env:LOCALAPPDATA 'xdclip'
$Lnk = Join-Path ([Environment]::GetFolderPath('Startup')) 'XD KVM Clipboard.lnk'
$mtx = New-Object Threading.Mutex($false, 'Local\xd-kvm-clip')
if (-not $mtx.WaitOne(0)) { exit }
Set-Content -Path (Join-Path $Dir 'pid') -Value $PID
$Max = 500000
$wc = New-Object Net.WebClient
$wc.Encoding = [Text.Encoding]::UTF8
$bi = 0; $seq = -1; $last = $null; $err = ''
while ($true) {
  $t = $null
  try { if ([Windows.Forms.Clipboard]::ContainsText()) { $t = [Windows.Forms.Clipboard]::GetText() } ; $err = '' }
  catch { $err = "doc clipboard: $($_.Exception.Message)" }
  if ($t -ne $null -and $t.Length -gt $Max) { $t = $t.Substring(0, $Max) }
  $b = @{ id = $env:COMPUTERNAME; host = $env:COMPUTERNAME; user = $env:USERNAME; os = 'windows'; v = 1; seq = $seq; err = $err }
  $send = ($t -ne $null -and $t -cne $last)
  if ($send) { $b.text = $t }
  try {
    $wc.Headers['Content-Type'] = 'application/json; charset=utf-8'
    $wc.Headers['X-Clip-Key'] = '__KEY__'
    $r = $wc.UploadString($Bases[$bi] + '/clip/agent', (ConvertTo-Json $b -Compress)) | ConvertFrom-Json
    if ($send) { $last = $t }
    if ($r.uninstall) {
      Remove-Item -Force -ErrorAction SilentlyContinue $Lnk
      Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $Dir
      exit
    }
    if ($r.push -ne $null -and $r.seq -ne $seq) {
      $p = [string]$r.push
      if ($p.Length -gt 0) {
        try { [Windows.Forms.Clipboard]::SetText($p); $last = $p } catch { $err = "ghi clipboard: $($_.Exception.Message)"; Start-Sleep -Milliseconds 300; continue }
      }
    }
    $seq = $r.seq
  } catch {
    $bi = ($bi + 1) % $Bases.Count
    Start-Sleep -Seconds 2
  }
  Start-Sleep -Milliseconds 600
}
