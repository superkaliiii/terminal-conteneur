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
$Launcher = "/root/.local/bin/$Tool"  # (PowerShell names ignore case: not $inside below)

function Distro-Exists { return ((& wsl.exe --list --quiet 2>$null) -replace "`0", "" | Where-Object { $_.Trim() -eq $Distro }) -ne $null }
function Has-Wsl { & wsl.exe --status 2>$null | Out-Null; return ($LASTEXITCODE -eq 0) }
function Deps-Missing {
    if (Has-Wsl) { return @() }
    return @("WSL (administrator; then restart Windows and run this installer again)")
}
function Install-Deps {
    # the features WSL 2 needs, through DISM: present on every Windows 10/11 (the inbox wsl.exe of older builds has
    # no --no-distribution, and a plain wsl --install would add an Ubuntu nobody asked for)
    if (-not (Has-Wsl)) {
        $dism = "/c dism /online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart && " +
                "dism /online /enable-feature /featurename:VirtualMachinePlatform /all /norestart"
        if (Run-Elevated $dism "cmd.exe") { $script:Restart = $true }
    }
}

function Install-Tool($src) {
    if ($script:Restart -or -not (Has-Wsl)) {
        Say "installation not finished: WSL is needed$(if ($script:Restart) { '; restart Windows' }), then run this installer again (it goes on from here)"
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
    # the tool inside, through its own installer (Python, Podman, openssl, iptables from Alpine's packages: the
    # distribution is terminal-ct's own, so that is part of this install); --exec: no shell re-reads the arguments
    $srcInWsl = (& wsl.exe -d $Distro --exec wslpath -a ($src -replace "\\", "/")) -replace "`0", ""
    $flags = @("--no-mcp") + $(if ($NoDeps) { @("--no-deps") } else { @("--yes") })
    & wsl.exe -d $Distro --cd / --exec sh "$srcInWsl/install.sh" @flags | Out-Host
    if ($LASTEXITCODE -ne 0) { Say "the installation inside $Distro stopped: run this installer again"; return $null }
    $cmd = Join-Path $Lib "$Tool.cmd"
    Write-Oem $cmd "@wsl.exe -d $Distro --cd ~ --exec $Launcher %*"
    return $cmd
}

function Mcp-Command { return @("wsl.exe", "-d", $Distro, "--exec", $Launcher, "mcp") }  # stdio, no network

function Install-Skill($src) {  # for Claude Code on Windows: its skills folder is Windows' own
    $dest = Join-Path $HOME ".claude\skills\$Tool"
    New-Item -ItemType Directory -Force $dest | Out-Null
    Copy-Item (Join-Path $src $Skill) (Join-Path $dest "SKILL.md") -Force
    return $true
}

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
function Write-Oem($path, $text) {  # what cmd.exe reads a .cmd in: the console's OEM code page
    $oem = [Text.Encoding]::GetEncoding([Globalization.CultureInfo]::CurrentCulture.TextInfo.OEMCodePage)
    [IO.File]::WriteAllText($path, $text + "`r`n", $oem)
}
function Run-Elevated($arguments, $exe = "powershell") {  # one UAC prompt; false when refused or failed
    try { $p = Start-Process $exe -Verb RunAs -Wait -PassThru -ArgumentList $arguments } catch { Say "not done: administrator rights refused"; return $false }
    if ($p.ExitCode -ne 0 -and $p.ExitCode -ne 3010) { Say "not done: $exe exited with $($p.ExitCode)"; return $false }
    return $true  # 3010: done, a restart finishes it
}
function Winget-Install($id) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) { Say "winget not found: install $id yourself"; return }
    & winget install --id $id -e --silent --accept-source-agreements --accept-package-agreements | Out-Host
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
}
function Find-Python {
    foreach ($c in @(@("py", "-3"), @("python"), @("python3"))) {
        if (-not (Get-Command $c[0] -ErrorAction SilentlyContinue)) { continue }
        $pre = @($c | Select-Object -Skip 1)
        $exe = & $c[0] @pre -c "import sys; print(sys.executable if sys.version_info >= (3, $PyMin) else '')" 2>$null
        if ($LASTEXITCODE -eq 0 -and $exe -and (Test-Path $exe)) { return $exe }  # not the Store's empty alias
    }
    return $null
}
function User-Path($add, $remove) {
    # the user's PATH as stored (REG_EXPAND_SZ: %USERPROFILE%\... stays a variable), then a broadcast for new windows
    $key = Get-Item "HKCU:\Environment"
    $raw = [string]$key.GetValue("Path", "", "DoNotExpandEnvironmentNames")
    $parts = @(($raw -split ";") | Where-Object { $_ -and $_ -ne $remove -and $_ -ne $add })
    if ($add) { $parts += $add }
    $kind = if ($raw -match "%" -or -not $raw) { "ExpandString" } else { $key.GetValueKind("Path") }  # as it was
    Set-ItemProperty "HKCU:\Environment" -Name Path -Value ($parts -join ";") -Type $kind
    [Environment]::SetEnvironmentVariable("TERMINAL_INSTALL_REFRESH", "1", "User")  # sends WM_SETTINGCHANGE
    [Environment]::SetEnvironmentVariable("TERMINAL_INSTALL_REFRESH", $null, "User")
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

# 1. what it needs: one question for all of it
$Python = if ($WinPython) { Find-Python } else { $null }
if (-not $NoDeps) {
    $missing = @(Deps-Missing)
    if ($WinPython -and -not $Python) { $missing = @("Python 3.$PyMin+ (winget)") + $missing }
    if ($missing.Count -and (Ask "$Tool needs: $($missing -join ', '). Install now?")) {
        if ($WinPython -and -not $Python) { Winget-Install "Python.Python.3.12"; $Python = Find-Python }
        Install-Deps
    } elseif ($missing.Count) { Say "not installed: $($missing -join ', ') ($Tool doctor says how)" }
}
if ($WinPython -and -not $Python) { throw "Python 3.$PyMin or later is required (winget install Python.Python.3.12)" }

# 2. the tool: from this folder when it holds it, else downloaded
$src = if ($PSScriptRoot -and (Test-Path (Join-Path $PSScriptRoot $File))) { $PSScriptRoot } else { $null }
$tmp = $null
if (-not $src) {
    $tmp = Join-Path ([IO.Path]::GetTempPath()) "$Tool-$PID"
    New-Item -ItemType Directory -Force $tmp | Out-Null
    Say "downloading $Tool..."
    Invoke-WebRequest -UseBasicParsing $Archive -OutFile "$tmp\a.zip"
    Expand-Archive "$tmp\a.zip" $tmp -Force
    $found = Get-ChildItem -Recurse -Path $tmp -Filter $File | Where-Object { (Split-Path $_.DirectoryName -Leaf) -eq $SubDir -or -not $SubDir } | Select-Object -First 1
    if (-not $found) { Remove-Item -Recurse -Force $tmp; throw "the download holds no $File" }
    $src = $found.DirectoryName
}
try {
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
    $Mcp = Mcp-Command
    if (-not $NoMcp -and (Get-Command claude -ErrorAction SilentlyContinue)) {
        & claude mcp remove $Tool -s user 2>$null | Out-Null  # an update: the path may have changed
        & claude mcp add -s user $Tool -- @Mcp | Out-Null
        if ($LASTEXITCODE -eq 0) { Say "MCP server $Tool declared in Claude Code" }
        else { Say "could not declare the MCP server: claude mcp add -s user $Tool -- $($Mcp -join ' ')" }
        if (Install-Skill $src) { Say "skill installed in ~\.claude\skills\$Tool" }
    } else {
        Say "for an agent: MCP server = $($Mcp -join ' ')"
    }
} finally {
    if ($tmp) { Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue }
}

# 5. what is left to fix, if anything
Say ""
& $Cmd doctor
if ($LASTEXITCODE -ne 0) { Say "doctor found something to fix: each line above gives the command" }
if ($script:Restart) { Say "restart Windows to finish (the Windows Hypervisor Platform, or WSL)" }
