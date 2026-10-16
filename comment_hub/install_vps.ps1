# Comment Hub - one-paste installer / updater for the VPS (run in an ADMINISTRATOR PowerShell):
#   irm https://raw.githubusercontent.com/Shani0017/fragspree-media/main/comment_hub/install_vps.ps1 | iex
# Safe to run again any time: it updates the code and the reel list, restarts the page, and keeps your .env and sign-in files.
$ErrorActionPreference = "Stop"
$base = "https://raw.githubusercontent.com/Shani0017/fragspree-media/main/comment_hub"
$dir = "C:\comment_hub"
New-Item -ItemType Directory -Force $dir | Out-Null
Write-Host "1/5 Downloading the page and the reel list..." -ForegroundColor Cyan
foreach ($f in "hub.py", "index.html", "video_context.json") {
    Invoke-WebRequest "${base}/${f}?t=$([DateTimeOffset]::UtcNow.ToUnixTimeSeconds())" -OutFile "$dir\$f" -UseBasicParsing
}
if (-not (Test-Path "$dir\cloudflared.exe")) {
    Write-Host "2/5 Downloading the secure tunnel (cloudflared, free, no account)..." -ForegroundColor Cyan
    Invoke-WebRequest "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe" -OutFile "$dir\cloudflared.exe" -UseBasicParsing
} else { Write-Host "2/5 Tunnel already there." -ForegroundColor Cyan }
Write-Host "3/5 Installing Python requests..." -ForegroundColor Cyan
python -m pip install --quiet requests
if (-not (Test-Path "$dir\.env")) {
    Write-Host "4/5 First time: choose settings" -ForegroundColor Cyan
    do { $pw = Read-Host "Choose a password for the page (at least 8 characters)" } while ($pw.Length -lt 8)
    $gem = Read-Host "Google Gemini key for AI replies (press Enter to skip, you can add it later)"
    @"
HUB_PASSWORD=$pw
HUB_PORT=8780
HUB_BIND=127.0.0.1
FRAG_ENV=C:\fragspree\.env
BRAIN_ENV=C:\brain_poster\.env
GEMINI_API_KEY=$gem
# 1 = TEST MODE (nothing is posted). Change to 0 when you are happy with the suggested replies.
DRY_RUN=1
IG_REPLY_CAP=120
YT_REPLY_CAP=60
PAUSE_MIN=12
PAUSE_MAX=35
DAYS=7
"@ | Set-Content -Encoding ASCII "$dir\.env"
} else { Write-Host "4/5 Keeping your existing .env" -ForegroundColor Cyan }
Write-Host "5/5 Starting the page and the tunnel..." -ForegroundColor Cyan
$py = (Get-Command python).Source
$s = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable
$t = New-ScheduledTaskTrigger -AtStartup
$a = New-ScheduledTaskAction -Execute $py -Argument "$dir\hub.py" -WorkingDirectory $dir
Register-ScheduledTask -TaskName CommentHub -Action $a -Trigger $t -Settings $s -RunLevel Highest -User SYSTEM -Force | Out-Null
$b = New-ScheduledTaskAction -Execute "$dir\cloudflared.exe" -Argument "tunnel --url http://127.0.0.1:8780 --logfile $dir\tunnel.log" -WorkingDirectory $dir
Register-ScheduledTask -TaskName CommentHubTunnel -Action $b -Trigger $t -Settings $s -RunLevel Highest -User SYSTEM -Force | Out-Null
Stop-ScheduledTask -TaskName CommentHub -ErrorAction SilentlyContinue
Start-Sleep 2
Start-ScheduledTask -TaskName CommentHub
if (-not (Get-Process cloudflared -ErrorAction SilentlyContinue)) { Start-ScheduledTask -TaskName CommentHubTunnel }
$url = $null
for ($i = 0; $i -lt 30 -and -not $url; $i++) {
    Start-Sleep 2
    if (Test-Path "$dir\tunnel.log") {
        $m = Select-String -Path "$dir\tunnel.log" -Pattern "https://[a-z0-9\-]+\.trycloudflare\.com" -ErrorAction SilentlyContinue | Select-Object -Last 1
        if ($m) { $url = $m.Matches[0].Value }
    }
}
Write-Host ""
if ($url) { Write-Host "YOUR PAGE: $url" -ForegroundColor Green } else { Write-Host "The tunnel is still starting. In a minute run:  Select-String -Path $dir\tunnel.log -Pattern trycloudflare.com | Select -Last 1" -ForegroundColor Yellow }
Write-Host "Open it on your phone and sign in with the password you chose. It starts in TEST MODE (nothing is posted)."
Write-Host "Still to do: (a) Instagram tokens with the comments permission, (b) paste the YouTube sign-in block from the PC (make_paste_for_vps.bat), (c) set DRY_RUN=0 in $dir\.env to go live." -ForegroundColor Cyan
