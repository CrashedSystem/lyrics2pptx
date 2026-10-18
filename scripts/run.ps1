<#
.SYNOPSIS
    lyrics2pptx local runner (Windows PowerShell 5.1+, no third-party modules).

.DESCRIPTION
    One entry point for every command declared in the environment manifest (section 3)
    and docs/arch/08 (section 1):

        .\scripts\run.ps1 -Doctor                          # diagnostics only  (DEFAULT)
        .\scripts\run.ps1 -Query "song title"              # search -> build output/<title>.pptx
        .\scripts\run.ps1 -LinesFile lines.txt -Output song.pptx
        .\scripts\run.ps1 -Web                             # local web UI, Ctrl+C stops it
        .\scripts\run.ps1 -Test                            # unittest regression suite

    Default behaviour: no switch and no -Query means -Doctor. -Doctor is read-only
    (writes no file, opens no socket), so a bare .\scripts\run.ps1 is always safe.

    Why this file is ASCII-only:
    Windows PowerShell 5.1 decodes a .ps1 file that has no BOM using the system ANSI
    code page, not UTF-8. This repository mandates UTF-8 *without* BOM (.editorconfig),
    so non-ASCII text in this file would be mojibaked under PS 5.1. Every message below
    is therefore ASCII; the Korean text the user sees is produced by the Python CLI,
    which is unaffected. The script pins the console and the child interpreter to UTF-8
    ([Console]::OutputEncoding + PYTHONIOENCODING=utf-8) so that Korean output survives
    a cp949 console.

    Exit codes are passed through unchanged from the child Python process
    (cli.py contract: 0 ok, 1 user error, 2 argparse, 3 empty lyrics, 4 template/save).
    This script adds one code of its own: 2 for an invalid script invocation, 127 when
    no Python interpreter can be found.

.PARAMETER Query
    Search term; runs: python -m lyrics2pptx.cli --query <Query>
.PARAMETER LinesFile
    UTF-8 file with one already-normalized line per line; normalization is skipped
    (python -m lyrics2pptx.cli --lines-file <LinesFile>).
.PARAMETER Web
    Start the local web UI (python -m lyrics2pptx.webapp, http://127.0.0.1:8765).
    Runs in the foreground; Ctrl+C stops the server.
.PARAMETER Doctor
    Read-only diagnostics (python -m lyrics2pptx.cli --doctor).
.PARAMETER Test
    Run the regression suite (python -m unittest discover -s tests -t .).
.PARAMETER Output
    Output path for -Query / -LinesFile. Omitted -> the pipeline default (output/<title>.pptx).
.PARAMETER Extra
    Extra arguments appended verbatim to the CLI call, e.g. -Extra '--dedupe-chorus'.

.EXAMPLE
    .\scripts\run.ps1 -Doctor

.EXAMPLE
    .\scripts\run.ps1 -Query "artist - title" -Output output\song.pptx -Extra '--strategy','zip'

.EXAMPLE
    .\scripts\run.ps1 -Test
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$Query,

    [string]$LinesFile,

    [switch]$Web,

    [switch]$Doctor,

    [switch]$Test,

    [string]$Output,

    [string[]]$Extra = @()
)

$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# 1. Encoding: keep Korean output readable on a cp949 console.
# ---------------------------------------------------------------------------
try {
    [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
    $OutputEncoding = New-Object System.Text.UTF8Encoding($false)
}
catch {
    Write-Host '[run] warning: could not switch the console to UTF-8 (continuing)'
}
$env:PYTHONIOENCODING = 'utf-8'

# ---------------------------------------------------------------------------
# 2. Always run from the repository root (scripts\run.ps1 -> root is its parent).
#    config.py derives every default path from the source tree, and the test
#    suite is discovered relative to the repo root, so the cwd must not be
#    inherited from the caller.
# ---------------------------------------------------------------------------
$RepoRoot = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot 'pyproject.toml'))) {
    Write-Host ("[run] error: pyproject.toml not found in '" + $RepoRoot + "'.")
    Write-Host '[run]        run.ps1 must stay in <repo>\scripts.'
    exit 2
}
Set-Location -LiteralPath $RepoRoot

# ---------------------------------------------------------------------------
# 3. Resolve the interpreter: repository venv > py launcher (-3.12) > python on PATH.
# ---------------------------------------------------------------------------
$PythonFile = $null
$PythonPrefix = @()

$VenvPython = Join-Path $RepoRoot '.venv\Scripts\python.exe'
if (Test-Path -LiteralPath $VenvPython) {
    $PythonFile = $VenvPython
}
else {
    $PyLauncher = Get-Command -Name 'py' -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($PyLauncher) {
        $PythonFile = $PyLauncher.Source
        $PythonPrefix = @('-3.12')
    }
    else {
        $PathPython = Get-Command -Name 'python' -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($PathPython) {
            $PythonFile = $PathPython.Source
        }
    }
}

if (-not $PythonFile) {
    Write-Host '[run] error: no Python interpreter found.'
    Write-Host '[run]        install Python 3.12 (winget install Python.Python.3.12) or create <repo>\.venv.'
    exit 127
}

# ---------------------------------------------------------------------------
# 4. Importability: if the package is not installed, fall back to the src layout
#    (docs/arch/08 section 1 mode B must always work, so the script works before
#    `python -m pip install -e .`).
# ---------------------------------------------------------------------------
& $PythonFile @PythonPrefix -c 'import lyrics2pptx' 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    if (-not $env:PYTHONPATH) {
        $env:PYTHONPATH = Join-Path $RepoRoot 'src'
        Write-Host '[run] lyrics2pptx is not installed: falling back to the src layout (PYTHONPATH=src).'
        Write-Host '[run] for a permanent install run: python -m pip install -e .'
    }
}

# ---------------------------------------------------------------------------
# 5. Pick exactly one mode. Ambiguity is refused, not guessed.
# ---------------------------------------------------------------------------
$Requested = @()
if ($PSBoundParameters.ContainsKey('Query')) { $Requested += 'query' }
if ($PSBoundParameters.ContainsKey('LinesFile')) { $Requested += 'linesfile' }
if ($Web) { $Requested += 'web' }
if ($Doctor) { $Requested += 'doctor' }
if ($Test) { $Requested += 'test' }

if ($Requested.Count -gt 1) {
    Write-Host ('[run] error: pick exactly one mode, got: ' + ($Requested -join ', '))
    Write-Host '[run]        modes: -Query | -LinesFile | -Web | -Doctor | -Test  (default: -Doctor)'
    exit 2
}

$Mode = 'doctor'
if ($Requested.Count -eq 1) { $Mode = $Requested[0] }

if ($Output -and $Mode -ne 'query' -and $Mode -ne 'linesfile') {
    Write-Host ('[run] error: -Output only applies to -Query / -LinesFile (mode: ' + $Mode + ').')
    exit 2
}
if ($Extra.Count -gt 0 -and $Mode -eq 'web') {
    Write-Host '[run] error: -Extra does not apply to -Web.'
    exit 2
}

# ---------------------------------------------------------------------------
# 6. Build the child command line. Validation of the values themselves belongs
#    to the CLI (single source of truth: cli.py), so nothing is pre-validated here.
# ---------------------------------------------------------------------------
$CliArgs = @()
if ($Mode -eq 'doctor') {
    $CliArgs = @('-m', 'lyrics2pptx.cli', '--doctor')
}
elseif ($Mode -eq 'query') {
    $CliArgs = @('-m', 'lyrics2pptx.cli', '--query', $Query)
}
elseif ($Mode -eq 'linesfile') {
    $CliArgs = @('-m', 'lyrics2pptx.cli', '--lines-file', $LinesFile)
}
elseif ($Mode -eq 'web') {
    $CliArgs = @('-m', 'lyrics2pptx.webapp')
}
elseif ($Mode -eq 'test') {
    $CliArgs = @('-m', 'unittest', 'discover', '-s', 'tests', '-t', '.')
}

if ($Output) { $CliArgs += @('-o', $Output) }
if ($Extra.Count -gt 0) { $CliArgs += $Extra }

# ---------------------------------------------------------------------------
# 7. Show what will run, run it, and hand the child's exit code back to the caller.
# ---------------------------------------------------------------------------
$ShownArgs = (@($PythonPrefix) + $CliArgs) -join ' '
Write-Host ('[run] cwd  = ' + $RepoRoot)
Write-Host ('[run] mode = ' + $Mode)
Write-Host ('[run] exec = ' + $PythonFile + ' ' + $ShownArgs)
if ($Mode -eq 'web') {
    Write-Host '[run] open http://127.0.0.1:8765 in a browser (Ctrl+C stops the server)'
}
Write-Host ''

& $PythonFile @PythonPrefix @CliArgs
exit $LASTEXITCODE
