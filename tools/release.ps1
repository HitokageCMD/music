#requires -Version 5.1
<#
  Publish a tunebox update. Bumps versionCode, builds the APK, and creates a
  GitHub Release tagged with the versionCode — which is exactly what the app's
  Updater checks. After the first release, every run of this makes phones
  running tunebox offer the update automatically.

  Prerequisites (one-time):
    1. A GitHub repo, and Updater.GITHUB_REPO set to "owner/repo".
    2. GitHub CLI installed + authed:  winget install GitHub.cli ;  gh auth login
    3. Run from the repo root:  tools\release.ps1
#>
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)

$gradleFile = 'android\app\build.gradle'
$g = Get-Content $gradleFile -Raw

# read + bump versionCode
if ($g -notmatch 'versionCode\s+(\d+)') { throw "versionCode not found in $gradleFile" }
$code = [int]$Matches[1] + 1
$g = $g -replace 'versionCode\s+\d+', "versionCode $code"
$g = $g -replace "versionName\s+'[^']*'", "versionName '0.$code'"
Set-Content $gradleFile $g -Encoding utf8 -NoNewline
Write-Host "versionCode -> $code" -ForegroundColor Yellow

# build
$env:JAVA_HOME = 'C:\Program Files\Eclipse Adoptium\jdk-17.0.18.8-hotspot'
$gradle = (Get-ChildItem "$env:USERPROFILE\.gradle\wrapper\dists\gradle-8.14.3-all" -Recurse -Filter gradle.bat | Select-Object -First 1).FullName
Push-Location android
& $gradle :app:assembleDebug --no-daemon --console=plain
Pop-Location
$apk = 'android\app\build\outputs\apk\debug\app-debug.apk'
if (-not (Test-Path $apk)) { throw 'build produced no APK' }
$named = "tunebox-0.$code-arm64.apk"
Copy-Item $apk $named -Force

# git commit + push (so the source matches the release)
git add -A
git commit -m "release v$code" 2>&1 | Out-Null
git push 2>&1 | Out-Null

# GitHub release, tag = versionCode (what Updater parses)
gh release create "v$code" $named --title "v$code" --notes "tunebox v0.$code"
Write-Host "released v$code — phones will pick it up within a day (or via 检查更新)" -ForegroundColor Green
