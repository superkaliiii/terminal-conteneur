# terminal-ct installer for Windows: its own WSL distribution (Alpine with Podman, apart from yours), the tool in it,
# a terminal-ct command on the Windows PATH, and Claude Code's MCP server and skill when Claude Code is there.
#   irm https://raw.githubusercontent.com/superkaliiii/terminal-conteneur/main/install.ps1 | iex
#   powershell -ExecutionPolicy Bypass -File install.ps1 [-Yes] [-NoDeps] [-NoMcp] [-Update] [-Uninstall]
# Through irm | iex options cannot be passed: set TERMINAL_CT_INSTALL="-Yes -NoMcp" (for example) first.
# Run it again after a failed or unfinished step (a restart for WSL): it goes on from where it stopped.
param([switch]$Yes, [switch]$NoDeps, [switch]$NoMcp, [switch]$Update, [switch]$Uninstall)
$Tool = "terminal-ct"
$File = "terminal_ct.py"
$Skill = "skills\terminal-ct\SKILL.md"
$Archive = if ($env:TERMINAL_CT_ARCHIVE) { $env:TERMINAL_CT_ARCHIVE } else { "https://github.com/superkaliiii/terminal-conteneur/archive/refs/heads/main.zip" }
$SubDir = ""
$PyMin = 9
$WinPython = $false  # it runs in its WSL distribution
$Opts = $env:TERMINAL_CT_INSTALL
$Distro = if ($env:TERMINAL_CT_DISTRO) { $env:TERMINAL_CT_DISTRO } else { "terminal-ct" }
$Alpine = "https://dl-cdn.alpinelinux.org/alpine/latest-stable/releases/x86_64"

function Distro-Exists { return ((& wsl.exe --list --quiet 2>$null) -replace "`0", "" | Where-Object { $_.Trim() -eq $Distro }) -ne $null }

function Install-Deps {
    # WSL itself: once per PC, with administrator rights and a restart
    & wsl.exe --status 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        if (Ask "terminal-ct runs in WSL, not installed here. Install WSL now (administrator; then restart Windows)?") {
            Start-Process wsl.exe -Verb RunAs -Wait -ArgumentList "--install --no-distribution"
        }
        $script:Restart = $true
    }
}

function Install-Tool($src) {
    if ($script:Restart) {
        Say "installation not finished: restart Windows, then run this installer again (it goes on from here)"
        return $null
    }
    New-Item -ItemType Directory -Force $Lib | Out-Null
    if (-not (Distro-Exists)) {  # its own distribution: Alpine's mini root file system, checked
        Say "creating the WSL distribution $Distro (Alpine)..."
        $index = (Invoke-WebRequest -UseBasicParsing "$Alpine/latest-releases.yaml").Content
        if ($index -is [byte[]]) { $index = [Text.Encoding]::UTF8.GetString($index) }  # served as an unknown type
        $name = ([regex]::Match($index, "file: (alpine-minirootfs-[0-9.]+-x86_64\.tar\.gz)")).Groups[1].Value
        $sha = ([regex]::Match($index, "file: $([regex]::Escape($name))[\s\S]*?sha256: ([0-9a-f]{64})")).Groups[1].Value
        if (-not $name -or -not $sha) { throw "Alpine's release list could not be read ($Alpine)" }
        $tar = Join-Path $Lib $name
        Invoke-WebRequest -UseBasicParsing "$Alpine/$name" -OutFile $tar
        if ((Get-FileHash -Algorithm SHA256 $tar).Hash.ToLower() -ne $sha) { Remove-Item $tar; throw "${name}: wrong checksum" }
        # its disk with the data, never in the program's folder (uninstall removes that one)
        $disk = Join-Path $env:LOCALAPPDATA "$Tool-wsl\$Distro"
        New-Item -ItemType Directory -Force $disk | Out-Null  # wsl --import makes no folder
        & wsl.exe --import $Distro $disk $tar --version 2 | Out-Host
        Remove-Item $tar
        if (-not (Distro-Exists)) { throw "wsl --import $Distro failed" }
    }
    # the tool inside, through its own installer (Python, Podman, openssl from Alpine's packages)
    $inside = (& wsl.exe -d $Distro -- wslpath -a ($src -replace "\\", "/")) -replace "`0", ""
    $flags = @("--no-mcp") + $(if ($NoDeps) { @("--no-deps") } else { @("--yes") })
    & wsl.exe -d $Distro --cd / -- sh "$inside/install.sh" @flags | Out-Host
    if ($LASTEXITCODE -ne 0) { Say "the installation inside $Distro stopped: run this installer again"; return $null }
    $cmd = Join-Path $Lib "$Tool.cmd"
    Set-Content -Encoding ascii $cmd "@wsl.exe -d $Distro --cd ~ -- /root/.local/bin/$Tool %*"
    return $cmd
}

function Mcp-Command { return @("wsl.exe", "-d", $Distro, "--", "/root/.local/bin/$Tool", "mcp") }  # stdio, no network

function Uninstall-Extra {
    if (Distro-Exists) {
        Say "kept: the WSL distribution $Distro, with its containers and their data. To delete it all: wsl --unregister $Distro"
    }
}

# ===== common to terminal-ct and terminal-vm's install.ps1 =====
if ($Opts) {
    foreach ($o in $Opts.Split(" ", [StringSplitOptions]::RemoveEmptyEntries)) {
        switch ($o) { "-Yes" { $Yes = $true } "-NoDeps" { $NoDeps = $true } "-NoMcp" { $NoMcp = $true }
                      "-Update" { $Update = $true } "-Uninstall" { $Uninstall = $true } default { throw "unknown option $o" } }
    }
}
$Lib = Join-Path $env:LOCALAPPDATA "Programs\$Tool"  # the program; its data (VMs, containers) is elsewhere, kept
$script:Restart = $false
function Say($m) { Write-Host $m }
function Ask($q) {
    if ($Yes) { return $true }
    if (-not [Environment]::UserInteractive) { Say "$q [skipped: no one to ask; run again with -Yes]"; return $false }
    $a = Read-Host "$q [Y/n]"
    return ($a -eq "" -or $a -match "^(y|yes|o|oui)$")
}
function Winget-Install($id) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) { Say "winget not found: install $id yourself"; return }
    & winget install --id $id -e --silent --accept-source-agreements --accept-package-agreements | Out-Host
    # what it put on the PATH, for this session too
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
}
function Find-Python {
    foreach ($c in @(@("py", "-3"), @("python"), @("python3"))) {
        if (-not (Get-Command $c[0] -ErrorAction SilentlyContinue)) { continue }
        $exe = & $c[0] @($c[1..9] | Where-Object { $_ }) -c "import sys; print(sys.executable if sys.version_info >= (3, $PyMin) else '')" 2>$null
        if ($LASTEXITCODE -eq 0 -and $exe -and (Test-Path $exe)) { return $exe }  # not the Store's empty alias
    }
    return $null
}
function User-Path($add, $remove) {
    $p = [Environment]::GetEnvironmentVariable("Path", "User")
    $parts = @(($p -split ";") | Where-Object { $_ -and $_ -ne $remove -and $_ -ne $add })
    if ($add) { $parts += $add }
    [Environment]::SetEnvironmentVariable("Path", ($parts -join ";"), "User")
}

if ($Uninstall) {
    if (-not $NoMcp -and (Get-Command claude -ErrorAction SilentlyContinue)) {
        & claude mcp remove $Tool -s user 2>$null | Out-Null
        Remove-Item -Recurse -Force (Join-Path $HOME ".claude\skills\$Tool") -ErrorAction SilentlyContinue
    }
    if (Get-Command Uninstall-Extra -ErrorAction SilentlyContinue) { Uninstall-Extra }
    Remove-Item -Recurse -Force $Lib -ErrorAction SilentlyContinue
    User-Path $null $Lib
    Say "$Tool removed. What it made stays (remove it first with $Tool itself if you want it gone)."
    return
}

# 1. what it needs
$Python = if ($WinPython) { Find-Python } else { $null }
if (-not $NoDeps) {
    if ($WinPython -and -not $Python -and (Ask "$Tool needs Python 3.$PyMin or later. Install it now (winget)?")) {
        Winget-Install "Python.Python.3.12"
        $Python = Find-Python
    }
    Install-Deps
}
if ($WinPython -and -not $Python) { throw "Python 3.$PyMin or later is required (winget install Python.Python.3.12)" }

# 2. the tool: from this folder when it holds it, else downloaded
$src = if ($PSScriptRoot -and (Test-Path (Join-Path $PSScriptRoot $File))) { $PSScriptRoot } else { $null }
if (-not $src) {
    $tmp = Join-Path ([IO.Path]::GetTempPath()) "$Tool-$PID"
    New-Item -ItemType Directory -Force $tmp | Out-Null
    Say "downloading $Tool..."
    Invoke-WebRequest -UseBasicParsing $Archive -OutFile "$tmp\a.zip"
    Expand-Archive "$tmp\a.zip" $tmp -Force
    $found = Get-ChildItem -Recurse -Path $tmp -Filter $File | Where-Object { (Split-Path $_.DirectoryName -Leaf) -eq $SubDir -or -not $SubDir } | Select-Object -First 1
    if (-not $found) { throw "the download holds no $File" }
    $src = $found.DirectoryName
}
$Cmd = Install-Tool $src
if (-not $Cmd) { return }  # not finished (a restart first): said why
Say "installed: $Cmd"

# 3. on the PATH (the user's: no administrator)
if (-not (($env:Path -split ";") -contains (Split-Path $Cmd))) {
    User-Path (Split-Path $Cmd) $null
    $env:Path += ";" + (Split-Path $Cmd)
    Say "added $(Split-Path $Cmd) to your PATH: open a new terminal to use $Tool"
}

# 4. the agent: Claude Code's MCP server and skill
$Mcp = if (Get-Command Mcp-Command -ErrorAction SilentlyContinue) { Mcp-Command } else { @($Cmd, "mcp") }
if (-not $NoMcp -and (Get-Command claude -ErrorAction SilentlyContinue)) {
    & claude mcp remove $Tool -s user 2>$null | Out-Null  # an update: the path may have changed
    & claude mcp add -s user $Tool -- @Mcp | Out-Null
    if ($LASTEXITCODE -eq 0) { Say "MCP server $Tool declared in Claude Code" }
    else { Say "could not declare the MCP server: claude mcp add -s user $Tool -- $($Mcp -join ' ')" }
    & $Cmd skills install 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { Say "skill installed in ~\.claude\skills\$Tool" }
} else {
    Say "for an agent: MCP server = $($Mcp -join ' ')"
}

# 5. what is left to fix, if anything
Say ""
& $Cmd doctor
if ($LASTEXITCODE -ne 0) { Say "doctor found something to fix: each line above gives the command" }
if ($script:Restart) { Say "restart Windows to finish turning on the Windows Hypervisor Platform" }
