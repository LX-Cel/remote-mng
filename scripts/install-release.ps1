<#+
Install a locally downloaded Windows release. The caller supplies its pinned
SHA256. This script executes no downloaded content until that checksum passes.
#>
param(
    [Parameter(Mandatory=$true)][string]$Archive,
    [Parameter(Mandatory=$true)][ValidatePattern('^[0-9a-fA-F]{64}$')][string]$Sha256,
    [string]$InstallDir,
    [string]$StateDir,
    [string]$ClaudeDir,
    [switch]$NoStart
)
$ErrorActionPreference = 'Stop'
$artifactPath = (Resolve-Path -LiteralPath $Archive).Path
if ((Get-FileHash -LiteralPath $artifactPath -Algorithm SHA256).Hash -ne $Sha256) {
    throw 'Artifact SHA256 mismatch; no downloaded program was executed.'
}
Add-Type -AssemblyName System.IO.Compression.FileSystem
$stageRoot = [System.IO.Path]::GetFullPath((Join-Path ([System.IO.Path]::GetTempPath()) ('rmg-bootstrap-' + [guid]::NewGuid().ToString('N'))))
[System.IO.Directory]::CreateDirectory($stageRoot) | Out-Null
try {
    $zip = [System.IO.Compression.ZipFile]::OpenRead($artifactPath)
    try {
        [long]$expandedBytes = 0
        $names = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
        foreach ($entry in $zip.Entries) {
            $name = $entry.FullName
            $expandedBytes += $entry.Length
            if ($name -match '(^[/\\]|\\|(^|/)\.\.?(/|$)|:)' -or -not $names.Add($name) -or $expandedBytes -gt 2147483648 -or $names.Count -gt 30000) {
                throw 'Unsafe ZIP contents.'
            }
            $targetPath = [System.IO.Path]::GetFullPath((Join-Path $stageRoot $name))
            if (-not $targetPath.StartsWith($stageRoot + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) { throw 'ZIP path escapes staging.' }
            $unixType = ($entry.ExternalAttributes -shr 16) -band 61440
            if ($unixType -eq 40960) { throw 'Archive links are forbidden.' }
        }
    } finally { $zip.Dispose() }
    [System.IO.Compression.ZipFile]::ExtractToDirectory($artifactPath, $stageRoot)
    $binary = Join-Path $stageRoot 'rmg.exe'
    if (-not (Test-Path -LiteralPath $binary -PathType Leaf)) { throw 'Expected rmg.exe at archive root.' }
    $installArgs = @('--json')
    if ($StateDir) { $installArgs += @('--home', $StateDir) }
    $installArgs += @('distribution', 'install', $artifactPath, '--sha256', $Sha256)
    if ($InstallDir) { $installArgs += @('--install-dir', $InstallDir) }
    if ($ClaudeDir) { $installArgs += @('--claude-dir', $ClaudeDir) }
    & $binary @installArgs
    if ($LASTEXITCODE -ne 0) { throw "Installation failed with exit code $LASTEXITCODE" }
    if (-not $NoStart) {
        $selectedRoot = if ($InstallDir) { [System.IO.Path]::GetFullPath($InstallDir) } else { Join-Path $env:LOCALAPPDATA 'remote-mng' }
        & (Join-Path $selectedRoot 'bin/start-manager.ps1')
        if ($LASTEXITCODE -ne 0) {
            throw "Tool and Skill installed, but manager startup failed. Run outside the Agent sandbox or double-click: $(Join-Path $selectedRoot 'bin/Open remote-mng.cmd')"
        }
    }
} finally {
    # Verify the absolute owned staging path before any recursive removal.
    $temporaryRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath()).TrimEnd('\','/')
    if ((Split-Path -Parent $stageRoot) -eq $temporaryRoot -and (Split-Path -Leaf $stageRoot) -match '^rmg-bootstrap-[0-9a-f]{32}$') {
        Remove-Item -LiteralPath $stageRoot -Recurse -Force
    }
}
