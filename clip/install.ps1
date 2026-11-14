# XD KVM clipboard agent - bo cai cho Windows (khong can quyen admin).
# Chep agent vao %LOCALAPPDATA%\xdclip, them shortcut an vao Startup, chay ngay.
try {
  $ErrorActionPreference = 'Stop'
  $B = '__BASE__'
  $D = Join-Path $env:LOCALAPPDATA 'xdclip'
  $F = Join-Path $D 'agent.ps1'
  New-Item -ItemType Directory -Force -Path $D | Out-Null
  $pf = Join-Path $D 'pid'
  if (Test-Path $pf) {
    $old = Get-Process -Id ([int](Get-Content $pf)) -ErrorAction SilentlyContinue
    if ($old -and $old.ProcessName -eq 'powershell') { Stop-Process -Id $old.Id -Force; Start-Sleep -Milliseconds 500 }
  }
  $wc = New-Object Net.WebClient
  $wc.Encoding = [Text.Encoding]::UTF8
  $src = $wc.DownloadString("$B__DL__/agent.ps1?b=$B")
  [IO.File]::WriteAllText($F, $src, (New-Object Text.UTF8Encoding $true))
  $conhost = Join-Path $env:WINDIR 'System32\conhost.exe'
  $a = "--headless powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$F`""
  $lnk = Join-Path ([Environment]::GetFolderPath('Startup')) 'XD KVM Clipboard.lnk'
  $s = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
  $s.TargetPath = $conhost; $s.Arguments = $a; $s.WindowStyle = 7
  $s.Description = 'Dong bo clipboard voi W105D KVM'; $s.Save()
  Start-Process -FilePath $conhost -ArgumentList $a -WindowStyle Hidden
  Write-Host 'XD KVM clipboard agent: DA CAI XONG' -ForegroundColor Green
  Start-Sleep -Seconds 2
  exit
} catch {
  Write-Host "XD KVM clipboard agent: LOI - $($_.Exception.Message)" -ForegroundColor Red
}
