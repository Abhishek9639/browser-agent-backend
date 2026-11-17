# Starts your own Chrome for the agent to drive (AGENT_CDP_URL, ADR-013).
#
# Sites such as Google refuse to sign anyone in to a browser that is being
# automated ("This browser or app may not be secure"). So sign in once with
# -SignIn, which opens the same profile as an ordinary Chrome with nothing
# attached, then close it and start it normally for the agent. The sign-ins
# stay in the profile, and the agent works already signed in.
#
#   .\scripts\chrome_debug.ps1 -SignIn   # once: sign in to your sites, then close the window
#   .\scripts\chrome_debug.ps1           # each session: leave it open while the agent runs
#
# Chrome 136+ refuses remote debugging on the default profile, so this uses a
# dedicated profile folder.
param(
    [switch]$SignIn,
    [int]$Port = 9222
)

$profileDir = Join-Path $env:LOCALAPPDATA "BAA-Chrome-Profile"
$candidates = @(
    (Join-Path $env:ProgramFiles "Google\Chrome\Application\chrome.exe"),
    (Join-Path ${env:ProgramFiles(x86)} "Google\Chrome\Application\chrome.exe"),
    (Join-Path $env:LOCALAPPDATA "Google\Chrome\Application\chrome.exe")
)
$chrome = $candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $chrome) {
    Write-Error "Chrome was not found. Install Google Chrome, or put its path in this script."
    exit 1
}

# A Chrome already running on this profile takes over the launch and ignores
# the flags below, so the debugging port would silently not open. Only the
# main process counts: helpers carry --type= and the same profile path.
$open = Get-CimInstance Win32_Process -Filter "Name = 'chrome.exe'" |
    Where-Object { $_.CommandLine -and $_.CommandLine.Contains($profileDir) -and $_.CommandLine -notmatch '--type=' }
if ($open) {
    if (-not $SignIn -and ($open | Where-Object { $_.CommandLine -match '--remote-debugging-port' })) {
        Write-Host "The agent's Chrome is already running with the agent connection. Nothing to do."
        exit 0
    }
    Write-Host "The agent's Chrome profile is already open (process $($open[0].ProcessId))."
    Write-Host "Close every window of it. If it keeps running after that, Chrome is staying open in"
    Write-Host "the background: right-click the Chrome icon by the clock (bottom right) and choose Exit."
    Write-Host "Then run this again."
    exit 1
}

if ($SignIn) {
    Write-Host "Opening the agent's Chrome profile as an ordinary browser, with nothing attached."
    Write-Host "Sign in to the sites you need (Google, IRCTC, MakeMyTrip...), then close the window."
    & $chrome "--user-data-dir=$profileDir"
    exit 0
}

Write-Host "Starting the agent's Chrome on port $Port..."
& $chrome "--remote-debugging-port=$Port" "--user-data-dir=$profileDir"

# Chrome returns at once, so wait until the agent can actually connect, and say so.
$url = "http://127.0.0.1:$Port/json/version"
for ($i = 0; $i -lt 15; $i++) {
    Start-Sleep -Seconds 1
    try {
        $version = Invoke-RestMethod -Uri $url -TimeoutSec 2
        Write-Host "Ready: $($version.Browser) is accepting the agent on $url"
        Write-Host "Leave this Chrome window open while the agent runs."
        Write-Host "The backend needs AGENT_CDP_URL=http://127.0.0.1:$Port in its .env (restart it after setting)."
        exit 0
    } catch {
        # Not up yet.
    }
}
Write-Host "Chrome started, but the agent connection did not open on port $Port."
Write-Host "Close every Chrome window of this profile (and Exit it from the icon by the clock), then run this again."
exit 1
