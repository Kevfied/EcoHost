# release.ps1 — build + sign + publish an EcoHost update to GitHub Releases.
#
# Trust model (ported from mc-modder): the signed latest.json manifest is the
# trust root — app clients fetch it + latest.json.minisig from the newest
# release, verify the minisign signature against the baked-in UPDATE_PUBKEY,
# then download the payload named inside (sha256-bound).
#
# Strict rule: NOTHING goes live until you answer "y" at the publish prompt.
#
# Usage:
#   .\release.ps1                    # build from tauri.conf.json version
#   .\release.ps1 -Bump patch        # bump 0.1.0 -> 0.1.1 everywhere, build
#   .\release.ps1 -SkipBuild         # reuse the newest installer on disk
#   .\release.ps1 -AllowSameVersion  # republish an identical version number
#   .\release.ps1 -Notes "..."       # release notes for the update card
#
# Signing key: the FIRST run generates one into %LOCALAPPDATA%\EcoHost\
# update-sign.key — keep it safe. The public key it prints must be pasted
# into UPDATE_PUBKEY in src-tauri/src/updater.rs before the first release
# clients can trust. A key inside this repository triggers a loud warning.
#
# Requires: gh (authenticated), plus `minisign`/`rsign` on PATH or just cargo
# (falls back to the repo's own relsign example).

param(
    [switch]$SkipBuild,
    [switch]$AllowSameVersion,
    [string]$Notes = "",
    [string]$Bump = "",
    [string]$KeyPath = ""
)

$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot
$TauriConf = Join-Path $Repo "src-tauri\tauri.conf.json"
$CargoToml = Join-Path $Repo "src-tauri\Cargo.toml"
$AppData = Join-Path $env:LOCALAPPDATA "EcoHost"
$GhRepo = "Kevfied/EcoHost"

function Fail($msg) { Write-Host "x $msg" -ForegroundColor Red; exit 1 }

# ---------------------------------------------------------------- signing tool
$Signer = $null
foreach ($c in "minisign", "rsign") {
    if (Get-Command $c -ErrorAction SilentlyContinue) { $Signer = $c; break }
}
if (-not $Signer) {
    if (Get-Command cargo -ErrorAction SilentlyContinue) {
        $Signer = "relsign"
    } else {
        Fail "no signer found. Install: winget install jedisct1.minisign  (or)  cargo install rsign2"
    }
}
function Invoke-SignGen($pub, $key) {
    if ($Signer -eq "minisign") { & minisign -G -p $pub -s $key }
    elseif ($Signer -eq "rsign") { & rsign generate -p $pub -s $key }
    else { & cargo run -q --manifest-path (Join-Path $Repo "src-tauri\Cargo.toml") --example relsign -- gen $pub $key }
}
function Invoke-Sign($key, $msg, $sigOut) {
    if ($Signer -eq "minisign") { & minisign -S -x $sigOut -s $key -m $msg }
    elseif ($Signer -eq "rsign") { & rsign sign -x $sigOut -s $key $msg }
    else { & cargo run -q --manifest-path (Join-Path $Repo "src-tauri\Cargo.toml") --example relsign -- sign $key $msg $sigOut }
}

# ---------------------------------------------------------------- gh sanity
if (-not (Get-Command gh -ErrorAction SilentlyContinue)) { Fail "gh CLI not installed" }

# ---------------------------------------------------------------- key
if (-not $KeyPath) {
    if ($env:ECOHOST_SIGN_KEY) { $KeyPath = $env:ECOHOST_SIGN_KEY }
    else { $KeyPath = Join-Path $AppData "update-sign.key" }
}
if (-not (Test-Path $KeyPath)) {
    New-Item -ItemType Directory -Force (Split-Path -Parent $KeyPath) | Out-Null
    Write-Host "Generating a new update signing key at $KeyPath"
    Invoke-SignGen (Join-Path $AppData "update-sign.pub") $KeyPath
    Write-Host ""
    Write-Host "!! Paste this public key into UPDATE_PUBKEY in src-tauri/src/updater.rs:"
    Get-Content (Join-Path $AppData "update-sign.pub")
    Write-Host ""
}
if ($KeyPath.StartsWith($Repo, [StringComparison]::OrdinalIgnoreCase)) {
    Write-Host "WARNING: signing key is inside the repository — move it out or it may leak." -ForegroundColor Yellow
    $w = Read-Host "continue anyway? [y/N]"
    if ($w -ne "y" -and $w -ne "Y") { Fail "aborted" }
}

# ---------------------------------------------------------------- version / build
if ($Bump) {
    $conf = Get-Content $TauriConf -Raw -Encoding UTF8 | ConvertFrom-Json
    $parts = $conf.version.Split(".")
    switch ($Bump) {
        "major" { $parts[0] = [int]$parts[0] + 1; $parts[1] = 0; $parts[2] = 0 }
        "minor" { $parts[1] = [int]$parts[1] + 1; $parts[2] = 0 }
        "patch" { $parts[2] = [int]$parts[2] + 1 }
        default { Fail "-Bump must be major|minor|patch" }
    }
    $newVer = $parts -join "."
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    foreach ($f in @($TauriConf, $CargoToml, (Join-Path $Repo "package.json"))) {
        $text = Get-Content $f -Raw -Encoding UTF8
        $text = $text -replace '(?m)("version"\s*:\s*|^version = )"[^"]+"', "`$1`"$newVer`""
        [System.IO.File]::WriteAllText($f, $text, $utf8NoBom)
    }
    Write-Host "Bumped version -> $newVer"
}
$Version = (Get-Content $TauriConf -Raw -Encoding UTF8 | ConvertFrom-Json).version
$Tag = "v$Version"

if (-not $SkipBuild) {
    Write-Host "Building backend sidecar (PyInstaller)…"
    Push-Location $Repo
    try { & pyinstaller ecohost-backend.spec --noconfirm } finally { Pop-Location }
    if ($LASTEXITCODE -ne 0) { Fail "pyinstaller failed" }
    $sidecar = Join-Path $Repo "src-tauri\binaries\ecohost-backend-x86_64-pc-windows-msvc.exe"
    New-Item -ItemType Directory -Force (Split-Path -Parent $sidecar) | Out-Null
    Copy-Item (Join-Path $Repo "dist\ecohost-backend.exe") $sidecar -Force

    Write-Host "Building app v$Version (npm run tauri build)…"
    Push-Location $Repo
    try { & npm run tauri build } finally { Pop-Location }
    if ($LASTEXITCODE -ne 0) { Fail "tauri build failed" }
}

# ---------------------------------------------------------------- payload + manifest
$NsisDir = Join-Path $Repo "src-tauri\target\release\bundle\nsis"
$Setup = Get-ChildItem $NsisDir -Filter "*_x64-setup.exe" -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $Setup) { Fail "no NSIS installer found in $NsisDir — build first" }
if ($Setup.Name -notlike "*_$Version`_*") {
    Write-Host "WARNING: newest installer is $($Setup.Name), expected *_$($Version)_* — using it anyway" -ForegroundColor Yellow
}

$Stage = Join-Path $env:TEMP "ecohost-release-$Version"
if (Test-Path $Stage) { Remove-Item -Recurse -Force $Stage }
New-Item -ItemType Directory -Force $Stage | Out-Null
Copy-Item $Setup.FullName (Join-Path $Stage $Setup.Name)

$manifest = [ordered]@{
    app     = "com.ecohost.app"
    version = $Version
    channel = "stable"
    arch    = "x86_64"
    notes   = $Notes
    pubDate = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    file    = $Setup.Name
    sha256  = (Get-FileHash $Setup.FullName -Algorithm SHA256).Hash.ToLower()
    size    = $Setup.Length
}
$manJson = $manifest | ConvertTo-Json -Compress
[System.IO.File]::WriteAllText((Join-Path $Stage "latest.json"), $manJson)

# Sign the manifest itself — the signature binds app+version+hash, so a
# forged version field or swapped payload fails verification.
Invoke-Sign $KeyPath (Join-Path $Stage "latest.json") (Join-Path $Stage "latest.json.minisig")
if ($LASTEXITCODE -ne 0) { Fail "signing failed" }
Write-Host "signed manifest -> v$Version ($($Setup.Name), $([math]::Round($Setup.Length/1MB,1)) MB)"

# ---------------------------------------------------------------- same-version guard
if (-not $AllowSameVersion) {
    $existing = & gh release view $Tag --repo $GhRepo --json tagName 2>$null
    if ($LASTEXITCODE -eq 0) {
        Fail "$Tag already exists on GitHub. Bump the version (or pass -AllowSameVersion for an emergency re-ship)."
    }
}

# ---------------------------------------------------------------- explicit publish
$ans = Read-Host "publish v$Version to GitHub Releases ($GhRepo)? [y/N]"
if ($ans -ne "y" -and $ans -ne "Y") { Fail "not published — nothing advertised" }

$releaseNotes = if ($Notes) { $Notes } else { "EcoHost v$Version" }
& gh release create $Tag --repo $GhRepo --title "EcoHost v$Version" --notes $releaseNotes `
    (Join-Path $Stage $Setup.Name) `
    (Join-Path $Stage "latest.json") `
    (Join-Path $Stage "latest.json.minisig")
if ($LASTEXITCODE -ne 0) { Fail "gh release create failed" }

Write-Host "✓ v$Version published to github.com/$GhRepo/releases" -ForegroundColor Green
Write-Host "  Running apps pick it up on their next update check."
