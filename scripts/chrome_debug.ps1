# Starts your own Chrome with remote debugging so the agent can drive it.
# Chrome 136+ refuses remote debugging on the default profile, so this uses a
# dedicated profile folder. Sign in to your sites once in that window.
$profile = Join-Path $env:LOCALAPPDATA "BAA-Chrome-Profile"
$chrome = "C:\Program Files\Google\Chrome\Application\chrome.exe"
& $chrome --remote-debugging-port=9222 "--user-data-dir=$profile"
