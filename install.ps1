<#
.SYNOPSIS
    Installs (or upgrades) Docker Monitor on Windows, then puts `docker-monitor` on your PATH.

.DESCRIPTION
    Uses uv if you have it, else pipx, else a private venv in %LOCALAPPDATA%\Programs\docker-monitor with a
    small docker-monitor.cmd shim added to your user PATH. Nothing needs admin rights.

        irm https://raw.githubusercontent.com/GD-HCK/docker-monitor/main/install.ps1 | iex

    To uninstall:

        & ([scriptblock]::Create((irm https://raw.githubusercontent.com/GD-HCK/docker-monitor/main/install.ps1))) -Uninstall
#>
[CmdletBinding()]
param(
    # Remove Docker Monitor
    [Parameter()]
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'

$repo = 'GD-HCK/docker-monitor'
$ref = if ($env:DOCKER_MONITOR_REF) { $env:DOCKER_MONITOR_REF } else { 'main' }
$source = if ($env:DOCKER_MONITOR_SOURCE) { $env:DOCKER_MONITOR_SOURCE } else { "https://github.com/$repo/archive/$ref.zip" }
$dataDir = Join-Path $env:LOCALAPPDATA 'Programs\docker-monitor'
$binDir = Join-Path $dataDir 'bin'

function Invoke-Native {
    param([Parameter(Mandatory)][string]$Command, [Parameter()][string[]]$Arguments)
    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Command $($Arguments -join ' ') exited with $LASTEXITCODE"
    }
}

function Test-Command {
    param([Parameter(Mandatory)][string]$Name)
    return $null -ne (Get-Command $Name -ErrorAction SilentlyContinue)
}

function Get-BasePython {
    foreach ($name in @('python', 'py', 'python3')) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($null -eq $cmd -or $cmd.Source -like '*\WindowsApps\python*.exe') {
            # Skip the Microsoft Store "install Python" stub.
            continue
        }
        & $cmd.Source -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>$null
        if ($LASTEXITCODE -eq 0) {
            return $cmd.Source
        }
    }
    throw 'Docker Monitor needs Python 3.10+ (or uv / pipx). Install one (e.g. winget install Python.Python.3.13) and re-run.'
}

function Set-UserPath {
    [CmdletBinding(SupportsShouldProcess)]
    param([Parameter(Mandatory)][string]$Directory, [Parameter()][switch]$Remove)
    $current = [Environment]::GetEnvironmentVariable('Path', 'User')
    $parts = @($current -split ';' | Where-Object { $_ -and $_ -ne $Directory })
    if (-not $Remove) {
        $parts += $Directory
    }
    if ($PSCmdlet.ShouldProcess('user PATH', "$(if ($Remove) { 'remove' } else { 'add' }) $Directory")) {
        [Environment]::SetEnvironmentVariable('Path', ($parts -join ';'), 'User')
    }
}

if ($Uninstall) {
    if (Test-Command 'uv') {
        & uv tool uninstall docker-monitor 2>$null | Out-Null
    }
    if (Test-Command 'pipx') {
        & pipx uninstall docker-monitor 2>$null | Out-Null
    }
    if (Test-Path $dataDir) {
        Remove-Item $dataDir -Recurse -Force
    }
    Set-UserPath -Directory $binDir -Remove
    Write-Host 'Docker Monitor uninstalled.' -ForegroundColor Green
    return
}

if (-not (Test-Command 'docker')) {
    Write-Host 'Heads up: the docker CLI is not on your PATH; Docker Monitor needs it to run.' -ForegroundColor Yellow
}

if (Test-Command 'uv') {
    Write-Host 'Installing with uv...' -ForegroundColor Green
    Invoke-Native -Command 'uv' -Arguments @('tool', 'install', '--force', '--quiet', "docker-monitor @ $source")
}
elseif (Test-Command 'pipx') {
    Write-Host 'Installing with pipx...' -ForegroundColor Green
    Invoke-Native -Command 'pipx' -Arguments @('install', '--force', $source)
}
else {
    $python = Get-BasePython
    $venvDir = Join-Path $dataDir 'venv'
    Write-Host 'Installing into a private venv in ' -ForegroundColor Green -NoNewline
    Write-Host $venvDir -ForegroundColor Magenta
    Invoke-Native -Command $python -Arguments @('-m', 'venv', $venvDir)
    $venvPython = Join-Path $venvDir 'Scripts\python.exe'
    Invoke-Native -Command $venvPython -Arguments @('-m', 'pip', 'install', '--quiet', '--disable-pip-version-check', '--upgrade', $source)

    # A .cmd shim keeps only docker-monitor on PATH, not the venv's python.exe.
    New-Item -ItemType Directory -Force -Path $binDir | Out-Null
    $shim = "@echo off`r`n`"$(Join-Path $venvDir 'Scripts\docker-monitor.exe')`" %*`r`n"
    Set-Content -Path (Join-Path $binDir 'docker-monitor.cmd') -Value $shim -NoNewline -Encoding ascii
    Set-UserPath -Directory $binDir
    $env:Path = "$env:Path;$binDir"
    Write-Host 'Added ' -ForegroundColor Green -NoNewline
    Write-Host $binDir -ForegroundColor Magenta -NoNewline
    Write-Host ' to your user PATH (new terminals pick it up).' -ForegroundColor Green
}

Write-Host 'Done. Run: ' -ForegroundColor Green -NoNewline
Write-Host 'docker-monitor' -ForegroundColor Magenta -NoNewline
Write-Host '   (or docker-monitor --snapshot for a one-shot summary)' -ForegroundColor Green
