# vedun launcher for Windows (Windows PowerShell 5.1 and PowerShell 7). Keep it next to compose.yaml in the install folder.
#
#   .\vedun.ps1 setup     first run: store the keys, choose the dashboard language
#   .\vedun.ps1 start     start the service and wait until it answers
#   .\vedun.ps1 help      all commands
#
# What it does, so you can check before running it: it never downloads or runs code, never changes system settings,
# the registry or PATH. Keys are encrypted with Windows DPAPI for your user only (files in %LOCALAPPDATA%\vedun\secrets)
# and are handed to `docker compose` for the duration of one command, then removed from the session again.
[CmdletBinding()]
param(
    [Parameter(Position = 0)] [string] $Command = 'help',
    [Parameter(Position = 1)] [string] $What = ''
)
$ErrorActionPreference = 'Stop'

$Version = '0.1.1'
$Dir = $PSScriptRoot
$Port = if ($env:VEDUN_PORT) { $env:VEDUN_PORT } else { '8765' }
$Url = "http://127.0.0.1:$Port"
$SecretDir = if ($env:VEDUN_SECRET_DIR) { $env:VEDUN_SECRET_DIR } else { Join-Path $env:LOCALAPPDATA 'vedun\secrets' }
$SecretNames = @('VEDUN_API_KEY', 'VEDUN_DASH_USER', 'VEDUN_DASH_PASSWORD', 'OPENROUTER_API_KEY', 'TYPESAFE_API_KEY')
$Required = @('VEDUN_API_KEY', 'VEDUN_DASH_USER', 'VEDUN_DASH_PASSWORD')

function Say([string] $Text, [string] $Color = 'Gray') { Write-Host $Text -ForegroundColor $Color }
function Ok([string] $Text) { Say "  [ok]   $Text" 'Green' }
function Warn([string] $Text) { Say "  [warn] $Text" 'Yellow' }
function Fail([string] $Text, [string] $Fix = '') {
    Say "  [fail] $Text" 'Red'
    if ($Fix) { Say "         -> $Fix" }
    exit 2
}

# ---------- secrets (DPAPI, current user) ----------
function Get-Secret([string] $Name) {
    $f = Join-Path $SecretDir "$Name.dpapi"
    if (-not (Test-Path $f)) { return '' }
    $secure = Get-Content -Raw $f | ConvertTo-SecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}
function Set-Secret([string] $Name, [string] $Value) {
    New-Item -ItemType Directory -Force -Path $SecretDir | Out-Null
    $Value | ConvertTo-SecureString -AsPlainText -Force | ConvertFrom-SecureString | Set-Content -NoNewline (Join-Path $SecretDir "$Name.dpapi")
}
function Remove-Secret([string] $Name) { Remove-Item -Force -ErrorAction SilentlyContinue (Join-Path $SecretDir "$Name.dpapi") }
function Read-Secret([string] $Prompt) {
    $s = Read-Host -AsSecureString $Prompt
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($s)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}
function New-RandomHex([int] $Bytes) {
    $b = New-Object byte[] $Bytes
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($b) } finally { $rng.Dispose() }
    return -join ($b | ForEach-Object { $_.ToString('x2') })
}
function New-Password {
    $chars = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789'
    $b = New-Object byte[] 64
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($b) } finally { $rng.Dispose() }
    return -join ($b[0..19] | ForEach-Object { $chars[$_ % $chars.Length] })
}

# ---------- docker ----------
function Test-Docker {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        Fail 'Docker is not installed.' 'Install Docker Desktop: https://docs.docker.com/desktop/setup/install/windows-install/'
    }
    docker info *> $null
    if ($LASTEXITCODE -ne 0) { Fail 'Docker Desktop is not running.' 'Start Docker Desktop, wait until it says "running", then try again.' }
}
function Test-Folder {
    if (-not (Test-Path (Join-Path $Dir 'compose.yaml'))) {
        Fail "compose.yaml is missing in $Dir." 'Keep vedun.ps1 in the install folder next to compose.yaml (see the README).'
    }
}
# Invoke-Compose <args>: the keys exist in the environment only while this one command runs
function Invoke-Compose([string[]] $ComposeArgs, [switch] $WithSecrets) {
    $set = @()
    try {
        $env:VEDUN_PORT = $Port
        $env:VEDUN_HOST_ROOT = Join-Path $Dir 'data'
        $env:VEDUN_HOST_PDF_DIR = Join-Path $Dir 'pdf'
        if ($WithSecrets) {
            foreach ($n in $SecretNames) {
                if (-not [Environment]::GetEnvironmentVariable($n)) {
                    $v = Get-Secret $n
                    if ($v) { [Environment]::SetEnvironmentVariable($n, $v); $set += $n }
                }
            }
        }
        docker compose --project-directory $Dir -f (Join-Path $Dir 'compose.yaml') @ComposeArgs
        if ($LASTEXITCODE -ne 0) { Fail "docker compose $($ComposeArgs -join ' ') failed." 'Read the message above; .\vedun.ps1 logs shows the service log.' }
    }
    finally {
        foreach ($n in $set) { [Environment]::SetEnvironmentVariable($n, $null) }
    }
}
function Test-Health {
    try { return (Invoke-WebRequest -UseBasicParsing -TimeoutSec 4 "$Url/api/health").StatusCode -eq 200 } catch { return $false }
}
function Wait-Health([int] $Seconds = 300) {
    for ($i = 0; $i -lt $Seconds; $i += 3) {
        if (Test-Health) { return $true }
        if ($i -gt 0 -and $i % 30 -eq 0) { Say "  still starting (${i}s; the first start downloads the image)" 'DarkGray' }
        Start-Sleep -Seconds 3
    }
    return $false
}

# ---------- commands ----------
function Invoke-Setup {
    Test-Folder
    foreach ($d in 'data', 'pdf', 'notes') { New-Item -ItemType Directory -Force -Path (Join-Path $Dir $d) | Out-Null }
    Say 'vedun setup' 'Cyan'
    Say '  OpenRouter key: makes embeddings for meaning-based search (get one at https://openrouter.ai/keys).'
    Say '  Without it, exact lookup and full-text search still work. Press Enter to skip.'
    $k = Read-Secret '  OpenRouter key'
    if ($k) { Set-Secret 'OPENROUTER_API_KEY' $k; Ok 'OpenRouter key stored' } else { Warn 'OpenRouter key skipped (add it later: .\vedun.ps1 keys openrouter)' }
    Say '  Typesafe key: optional reranker for better ordering (https://typesafe.ai). Press Enter to skip.'
    $k = Read-Secret '  Typesafe key'
    if ($k) { Set-Secret 'TYPESAFE_API_KEY' $k; Ok 'Typesafe key stored' } else { Warn 'Typesafe key skipped' }
    if (Get-Secret 'VEDUN_API_KEY') { Ok 'API key kept (connected agents keep working)' } else { Set-Secret 'VEDUN_API_KEY' (New-RandomHex 32); Ok 'API key generated' }
    if (-not (Get-Secret 'VEDUN_DASH_USER')) { Set-Secret 'VEDUN_DASH_USER' 'vedun' }
    if (Get-Secret 'VEDUN_DASH_PASSWORD') { Ok 'dashboard password kept' } else { Set-Secret 'VEDUN_DASH_PASSWORD' (New-Password); Ok 'dashboard password generated' }
    $conf = Join-Path $Dir 'data\vedun.config.json'
    if (Test-Path $conf) { Ok "profile kept: $conf" } else {
        $lang = if ((Get-Culture).Name -like 'pl*') { 'pl' } else { 'en' }
        $ans = Read-Host "  Dashboard language (en/pl) [$lang]"
        if ($ans -in @('en', 'pl')) { $lang = $ans }
        [IO.File]::WriteAllText($conf, "{`"ui_language`": `"$lang`"}`n")
        Ok "dashboard language: $lang"
    }
    Say ''
    Say "  Keys are encrypted for your Windows user in $SecretDir."
    Say '  Next: .\vedun.ps1 start, then .\vedun.ps1 open. Your login: .\vedun.ps1 keys show'
}
function Invoke-Start {
    Test-Folder; Test-Docker
    foreach ($n in $Required) {
        if (-not ([Environment]::GetEnvironmentVariable($n) -or (Get-Secret $n))) { Fail "$n is not set." 'Run .\vedun.ps1 setup first.' }
    }
    Invoke-Compose @('up', '-d') -WithSecrets
    if (Wait-Health) { Ok "vedun is running: $Url" } else { Fail "vedun did not answer on $Url/api/health." '.\vedun.ps1 logs shows why.' }
}
function Invoke-Status {
    Test-Folder
    if (Test-Health) { Ok "vedun answers: $Url" } else { Warn "vedun does not answer on $Url (.\vedun.ps1 start)" }
    foreach ($n in $SecretNames) {
        $has = [bool]([Environment]::GetEnvironmentVariable($n) -or (Get-Secret $n))
        Say ('  {0,-22} {1}' -f $n, $(if ($has) { 'set' } else { 'not set' }))
    }
}
function Invoke-Keys {
    switch ($What) {
        'openrouter' { $k = Read-Secret 'OpenRouter key (Enter = remove)'; if ($k) { Set-Secret 'OPENROUTER_API_KEY' $k; Ok 'stored' } else { Remove-Secret 'OPENROUTER_API_KEY'; Warn 'removed' } }
        'typesafe' { $k = Read-Secret 'Typesafe key (Enter = remove)'; if ($k) { Set-Secret 'TYPESAFE_API_KEY' $k; Ok 'stored' } else { Remove-Secret 'TYPESAFE_API_KEY'; Warn 'removed' } }
        'password' {
            $p = Read-Secret 'New dashboard password (Enter = generate one)'
            if (-not $p) { $p = New-Password }
            Set-Secret 'VEDUN_DASH_PASSWORD' $p; Ok 'dashboard password changed'
        }
        'api-key' {
            Warn 'A new API key disconnects every AI agent that uses the current one.'
            if ((Read-Host '  Create a new API key? [y/N]') -match '^(y|yes|t|tak)$') { Set-Secret 'VEDUN_API_KEY' (New-RandomHex 32); Ok 'new API key stored; update your agents (.\vedun.ps1 keys show)' }
        }
        'show' {
            Say "  Dashboard   $Url"
            Say "  User        $(Get-Secret 'VEDUN_DASH_USER')"
            Say "  Password    $(Get-Secret 'VEDUN_DASH_PASSWORD')"
            Say "  API key     $(Get-Secret 'VEDUN_API_KEY')"
            return
        }
        default { Fail "Say which key: openrouter, typesafe, password, api-key or show." '.\vedun.ps1 keys show' }
    }
    if (Test-Health) { Say '  applying: restarting with the new keys'; Invoke-Compose @('up', '-d') -WithSecrets }
}

switch ($Command) {
    'setup' { Invoke-Setup }
    'start' { Invoke-Start }
    'stop' { Test-Folder; Invoke-Compose @('stop'); Ok 'vedun stopped' }
    'status' { Invoke-Status }
    'logs' { Test-Folder; Invoke-Compose @('logs', '-f', '--tail', '100') }
    'update' { Test-Folder; Test-Docker; Invoke-Compose @('pull'); Invoke-Start }
    'open' { Start-Process $Url }
    'keys' { Invoke-Keys }
    'version' { Say "vedun $Version" }
    default {
        Say "vedun $Version - local document knowledge base (dashboard, API, MCP server)" 'Cyan'
        Say '  .\vedun.ps1 setup          store the keys, choose the language (first run)'
        Say '  .\vedun.ps1 start | stop   start or stop the service'
        Say '  .\vedun.ps1 status         does it answer, which keys are set'
        Say '  .\vedun.ps1 logs           follow the service log'
        Say '  .\vedun.ps1 update         download the new image and restart'
        Say '  .\vedun.ps1 open           open the dashboard'
        Say '  .\vedun.ps1 keys openrouter|typesafe|password|api-key|show'
    }
}
