<#
.SYNOPSIS
Install or update remote-mng and its Claude Code Skill for the current user.
.DESCRIPTION
Download this file and run it in an ordinary Windows PowerShell terminal.
No Python, uv, GitHub CLI, administrator account, or manually selected ZIP is
required. Downloads are pinned to one GitHub release and SHA256 is checked
before any downloaded program runs. Re-running this installer also updates an
existing supported installation. It never forcibly disconnects remote sessions.
.EXAMPLE
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
.EXAMPLE
.\install.ps1 -Preview
#>
[CmdletBinding()]
param(
    [ValidatePattern('^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$')][string]$Version,
    [switch]$Preview,
    [string]$InstallDir,
    [string]$StateDir,
    [string]$ClaudeDir,
    [switch]$NoStart,
    [ValidatePattern('^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$')][string]$Repository = 'LX-Cel/remote-mng'
)

$ErrorActionPreference = 'Stop'

function Get-RmgProperty {
    param($Object, [string]$Name)
    if ($null -ne $Object -and $null -ne $Object.PSObject.Properties[$Name]) {
        return $Object.PSObject.Properties[$Name].Value
    }
    return $null
}

function Get-RmgJson {
    param([string]$Uri, [switch]$AllowNotFound)
    try {
        return Invoke-RestMethod -Uri $Uri -Headers @{
            Accept = 'application/vnd.github+json'
            'User-Agent' = 'remote-mng-installer'
            'X-GitHub-Api-Version' = '2022-11-28'
        } -TimeoutSec 60
    } catch {
        if ($AllowNotFound -and $_.Exception.Response -and [int]$_.Exception.Response.StatusCode -eq 404) {
            return $null
        }
        throw 'Could not read the public GitHub release. Check your connection, repository and version, or try again after the GitHub API rate limit resets. A private repository requires the authenticated installation workflow.'
    }
}

function Compare-RmgVersion {
    param([string]$Left, [string]$Right)
    $a = $Left.Substring(1).Split('.')
    $b = $Right.Substring(1).Split('.')
    for ($index = 0; $index -lt 3; $index++) {
        if ($a[$index].Length -ne $b[$index].Length) {
            return [Math]::Sign($a[$index].Length - $b[$index].Length)
        }
        $comparison = [string]::CompareOrdinal($a[$index], $b[$index])
        if ($comparison -ne 0) { return [Math]::Sign($comparison) }
    }
    return 0
}

function Get-RmgRelease {
    param([string]$Repository, [string]$Version, [switch]$Preview)
    if ($Version -and $Preview) { throw 'Choose either -Version or -Preview, not both.' }
    $base = 'https://api.github.com/repos/' + $Repository + '/releases'
    $versionPattern = '^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$'
    if ($Version) {
        if ((Compare-RmgVersion $Version 'v0.5.0') -lt 0) {
            throw 'This installer supports remote-mng v0.5.0 and newer. To install an older version, use its archived installation guide. No installation was changed.'
        }
        $release = Get-RmgJson ($base + '/tags/' + $Version)
        if ((Get-RmgProperty $release 'tag_name') -ne $Version) {
            throw 'The release returned by GitHub does not match the requested version.'
        }
    } else {
        $release = if ($Preview) { $null } else { Get-RmgJson ($base + '/latest') -AllowNotFound }
        if ($null -ne $release -and (Get-RmgProperty $release 'prerelease')) {
            throw 'GitHub returned a preview for the stable channel. Use -Preview only if you want a preview.'
        }
        # Older published ZIPs do not contain the unified bootstrap entry point.
        # Prefer stable as soon as a compatible stable release exists; otherwise
        # the first compatible preview is an explicit, visible bootstrap choice.
        if ($null -ne $release -and (Get-RmgProperty $release 'tag_name') -match $versionPattern -and
                (Compare-RmgVersion $release.tag_name 'v0.5.0') -lt 0) {
            $release = $null
        }
        if ($null -eq $release) {
            for ($page = 1; $page -le 10; $page++) {
                $items = @(Get-RmgJson ($base + '?per_page=100&page=' + $page))
                foreach ($item in $items) {
                    $tag = Get-RmgProperty $item 'tag_name'
                    if ((Get-RmgProperty $item 'draft') -or $tag -notmatch $versionPattern) { continue }
                    if ((Compare-RmgVersion $tag 'v0.5.0') -lt 0) { continue }
                    if ($null -eq $release -or (Compare-RmgVersion $tag $release.tag_name) -gt 0) {
                        $release = $item
                    }
                }
                if ($items.Count -lt 100) { break }
                if ($page -eq 10) { throw 'Too many releases to select a preview safely. Use -Version with an exact release tag.' }
            }
        }
    }
    if ($null -eq $release -or (Get-RmgProperty $release 'draft') -or
            (Get-RmgProperty $release 'tag_name') -notmatch $versionPattern) {
        throw 'No compatible published release was found. This installer requires v0.5.0 or newer. No installation was changed.'
    }
    return $release
}

function Get-RmgPlatform {
    $architecture = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
    if ($architecture -ne 'AMD64') {
        throw ('This installer needs a Windows x64 release; this system reports ' + $architecture + '. No installation was changed.')
    }
    return 'windows-x86_64'
}

function Get-RmgAsset {
    param($Release, [string]$Repository, [string]$Name)
    $matches = @((Get-RmgProperty $Release 'assets') | Where-Object { $_.name -ceq $Name })
    if ($matches.Count -ne 1) { throw ('Release asset is missing or ambiguous: ' + $Name + '. No installation was changed.') }
    $expected = 'https://github.com/' + $Repository + '/releases/download/' + $Release.tag_name + '/' + $Name
    if ((Get-RmgProperty $matches[0] 'browser_download_url') -cne $expected) {
        throw 'The release asset has an unexpected download address. No downloaded program was executed.'
    }
    return $matches[0]
}

function Receive-RmgFile {
    param([string]$Uri, [string]$Destination, [long]$MaximumBytes)
    $partial = $Destination + '.part'
    try {
        Invoke-WebRequest -UseBasicParsing -Uri $Uri -OutFile $partial -Headers @{
            'User-Agent' = 'remote-mng-installer'
        } -TimeoutSec 300 | Out-Null
        if ((Get-Item -LiteralPath $partial).Length -gt $MaximumBytes) {
            throw 'The downloaded file exceeds the installation limit.'
        }
        Move-Item -LiteralPath $partial -Destination $Destination
    } catch {
        throw ('Could not download the release asset. No installation was changed. ' + $_.Exception.Message)
    } finally {
        if (Test-Path -LiteralPath $partial) { Remove-Item -LiteralPath $partial -Force }
    }
}

function Expand-RmgArchive {
    param([string]$Archive, [string]$Destination)
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $root = [IO.Path]::GetFullPath($Destination).TrimEnd('\', '/')
    $zip = [IO.Compression.ZipFile]::OpenRead($Archive)
    try {
        [long]$expandedBytes = 0
        $names = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
        foreach ($entry in $zip.Entries) {
            $name = $entry.FullName.TrimEnd('/')
            $expandedBytes += $entry.Length
            if (-not $name -or $name -match '(^[/\\]|\\|(^|/)\.\.?(/|$)|:|[\x00-\x1f])' -or
                    -not $names.Add($name) -or $expandedBytes -gt 2147483648 -or $names.Count -gt 30000) {
                throw 'Unsafe ZIP contents; no downloaded program was executed.'
            }
            foreach ($part in $name.Split('/')) {
                if (-not $part -or $part -match '[. ]$' -or $part -match '^(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)') {
                    throw 'Unsafe ZIP path; no downloaded program was executed.'
                }
            }
            $target = [IO.Path]::GetFullPath((Join-Path $root $name))
            if (-not $target.StartsWith($root + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
                throw 'ZIP path escapes staging; no downloaded program was executed.'
            }
            $unixType = ($entry.ExternalAttributes -shr 16) -band 61440
            if ($unixType -notin @(0, 32768, 16384)) {
                throw 'Archive links and special files are forbidden; no downloaded program was executed.'
            }
        }
    } finally { $zip.Dispose() }
    [IO.Compression.ZipFile]::ExtractToDirectory($Archive, $root)
    $program = Join-Path $root 'rmg.exe'
    if (-not (Test-Path -LiteralPath $program -PathType Leaf)) {
        throw 'The verified release has no rmg.exe at its root.'
    }
    return $program
}

function Get-RmgSha256 {
    param([string]$Path)
    $inputStream = [IO.File]::OpenRead($Path)
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($algorithm.ComputeHash($inputStream))).Replace('-', '').ToLowerInvariant()
    } finally {
        $algorithm.Dispose()
        $inputStream.Dispose()
    }
}

function Invoke-RmgBootstrapProgram {
    param([string]$Program, [string[]]$Arguments)
    # The program emits UTF-8 JSON even on legacy Windows terminals.
    $oldEncoding = [Console]::OutputEncoding
    try {
        [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
        $output = @(& $Program @Arguments)
        $exitCode = $LASTEXITCODE
    } finally { [Console]::OutputEncoding = $oldEncoding }
    try { $result = ($output -join "`n") | ConvertFrom-Json } catch {
        throw 'The installer did not return a valid result. Inspect the local installation before retrying.'
    }
    if ($exitCode -ne 0 -or $null -ne (Get-RmgProperty $result 'error')) {
        $errorRecord = Get-RmgProperty $result 'error'
        $message = Get-RmgProperty $errorRecord 'message'
        $code = Get-RmgProperty $errorRecord 'code'
        if (-not $message) { $message = 'The update could not finish. Inspect its status before retrying.' }
        $details = Get-RmgProperty $errorRecord 'details'
        $advice = Get-RmgProperty $details 'advice'
        if ($advice) { $message += ' ' + $advice }
        if ($code) { $message += ' [' + $code + ']' }
        throw $message
    }
    return $result
}

function Install-RemoteMng {
    [CmdletBinding()]
    param([string]$Version, [switch]$Preview, [string]$InstallDir, [string]$StateDir,
        [string]$ClaudeDir, [switch]$NoStart, [string]$Repository = 'LX-Cel/remote-mng')
    if ($Repository -notmatch '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$' -or
            ($Version -and $Version -notmatch '^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$')) {
        throw 'Use a GitHub OWNER/REPOSITORY and an exact vMAJOR.MINOR.PATCH version.'
    }
    # TLS 1.2 is needed by Windows PowerShell 5.1 on older Windows systems.
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    $platform = Get-RmgPlatform
    Write-Host 'Checking the remote-mng release...'
    $release = Get-RmgRelease -Repository $Repository -Version $Version -Preview:$Preview
    $tag = [string]$release.tag_name
    if (Get-RmgProperty $release 'prerelease') {
        Write-Host ('The selected release ' + $tag + ' is a preview version. Its update channel will be recorded as preview.') -ForegroundColor Yellow
    }
    $assetName = 'remote-mng-' + $tag.Substring(1) + '-' + $platform + '.zip'
    $asset = Get-RmgAsset $release $Repository $assetName
    $checksum = Get-RmgAsset $release $Repository ($assetName + '.sha256')
    $selectedRoot = if ($InstallDir) { [IO.Path]::GetFullPath($InstallDir) } else {
        $local = [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData)
        if ($env:LOCALAPPDATA) { $local = $env:LOCALAPPDATA }
        Join-Path $local 'remote-mng'
    }
    $temporaryRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\', '/')
    $work = Join-Path $temporaryRoot ('rmg-install-' + [guid]::NewGuid().ToString('N'))
    [IO.Directory]::CreateDirectory($work) | Out-Null
    try {
        $archive = Join-Path $work $assetName
        $checksumPath = $archive + '.sha256'
        Write-Host ('Downloading and verifying remote-mng ' + $tag + '...')
        Receive-RmgFile $checksum.browser_download_url $checksumPath 4096
        $checksumText = [IO.File]::ReadAllText($checksumPath).Trim()
        if ($checksumText -notmatch '^([0-9a-fA-F]{64})[ \t]+\*?([^\r\n]+)$' -or $Matches[2] -cne $assetName) {
            throw 'The release checksum is invalid or names another asset. No downloaded program was executed.'
        }
        $digest = $Matches[1].ToLowerInvariant()
        Receive-RmgFile $asset.browser_download_url $archive 1073741824
        if ((Get-RmgSha256 $archive) -cne $digest) {
            throw 'Release SHA256 mismatch; no downloaded program was executed.'
        }
        $candidate = Expand-RmgArchive $archive (Join-Path $work 'candidate')
        $arguments = @('--json')
        if ($StateDir) { $arguments += @('--home', $StateDir) }
        $arguments += @('bootstrap', $archive, '--sha256', $digest, '--repository', $Repository, '--tag', $tag)
        # Omitted context belongs to the Python installer: it can discover the
        # existing Skill binding and retain a previous custom installation.
        if ($InstallDir) { $arguments += @('--install-dir', $selectedRoot) }
        if ($ClaudeDir) { $arguments += @('--claude-dir', $ClaudeDir) }
        if ($NoStart) { $arguments += '--no-start' }
        if ($Preview -or (Get-RmgProperty $release 'prerelease')) { $arguments += '--preview' }
        Write-Host 'Installing the program and Claude Code Skill. Existing activity is checked before switching versions...'
        $result = Invoke-RmgBootstrapProgram $candidate $arguments
        $state = Get-RmgProperty $result 'state'
        if ($state -ne 'succeeded') {
            $message = Get-RmgProperty $result 'message'
            if (-not $message) { $message = 'Installation did not finish. Inspect its status before retrying.' }
            throw $message
        }
        $actualVersion = Get-RmgProperty $result 'version'
        if (-not $actualVersion) { $actualVersion = Get-RmgProperty $result 'current_version' }
        if (-not $actualVersion) { $actualVersion = $tag }
        Write-Host ('remote-mng ' + $actualVersion + ' is ready.')
        $actualRoot = Get-RmgProperty $result 'install_dir'
        if (-not $actualRoot) { $actualRoot = $selectedRoot }
        Write-Host ('Installed for this user: ' + $actualRoot)
        Write-Host 'Open a new Claude Code session and ask: Use remote-mng, check the installation and open the status console.'
        return $result
    } finally {
        # Only remove the randomly named directory created by this invocation.
        $resolvedWork = [IO.Path]::GetFullPath($work)
        if ((Split-Path -Parent $resolvedWork) -eq $temporaryRoot -and
                (Split-Path -Leaf $resolvedWork) -match '^rmg-install-[0-9a-f]{32}$') {
            Remove-Item -LiteralPath $resolvedWork -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    try {
        Install-RemoteMng @PSBoundParameters | Out-Null
    } catch {
        Write-Host ('remote-mng installation stopped: ' + $_.Exception.Message) -ForegroundColor Red
        exit 1
    }
}
