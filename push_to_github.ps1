# push_to_github.ps1 - one-time upload of this folder to your GitHub repo.
# PREREQS: (1) Git for Windows installed, (2) an EMPTY GitHub repo created at github.com.
# USAGE (from this folder), with your REAL username (no angle brackets):
#   .\push_to_github.ps1 -RepoUrl https://github.com/YOURNAME/deen-dashboard.git
# On the first push a browser opens to sign in to GitHub (Git Credential Manager).

param(
  [Parameter(Mandatory=$true)][string]$RepoUrl
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if ($RepoUrl -match '[<>]') {
  Write-Host "Put your REAL GitHub username in the URL - it must contain no < or > characters." -ForegroundColor Red
  Write-Host "Example: .\push_to_github.ps1 -RepoUrl https://github.com/shaq/deen-dashboard.git"
  exit 1
}

# Resolve git: prefer PATH, else fall back to the standard install location
# (a freshly-installed Git isn't on THIS shell's PATH until you open a new terminal).
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
  $gitDir = @("$env:ProgramFiles\Git\cmd", "${env:ProgramFiles(x86)}\Git\cmd",
              "$env:LOCALAPPDATA\Programs\Git\cmd") |
            Where-Object { Test-Path (Join-Path $_ 'git.exe') } | Select-Object -First 1
  if ($gitDir) {
    $env:PATH = "$gitDir;$env:PATH"
    Write-Host "Using Git at $gitDir (not yet on this shell's PATH)." -ForegroundColor DarkGray
  } else {
    Write-Host "Git is not installed. Get 'Git for Windows' from https://git-scm.com/download/win then re-run." -ForegroundColor Red
    exit 1
  }
}

if (-not (Test-Path ".git")) { git init | Out-Null }

# Ensure a commit identity exists (local to THIS repo only) so the commit succeeds,
# derived from the owner in the repo URL. Skips if you already have one configured.
if (-not (git config user.email)) {
  $owner = ($RepoUrl -replace '.*github\.com[:/]','') -replace '/.*',''
  if (-not $owner) { $owner = "deen-capital" }
  git config user.name  $owner
  git config user.email "$owner@users.noreply.github.com"
  Write-Host "Set repo commit identity to $owner." -ForegroundColor DarkGray
}

git add -A
git commit -m "Deen Capital cloud dashboard - initial" | Out-Null
git branch -M main
if (git remote | Select-String -SimpleMatch origin) { git remote remove origin }
git remote add origin $RepoUrl
git push -u origin main

Write-Host ""
Write-Host "Pushed. Now finish in the browser:" -ForegroundColor Green
Write-Host "  1. Repo Settings -> Pages -> Source: 'Deploy from a branch' -> Branch: main / (root) -> Save"
Write-Host "  2. Repo Actions tab -> if prompted, click 'I understand, enable workflows'"
Write-Host "  3. Actions -> 'Update dashboard' -> 'Run workflow' to fill the last few days"
Write-Host "  Your live page (ready in ~1 min): https://YOURNAME.github.io/deen-dashboard/"
