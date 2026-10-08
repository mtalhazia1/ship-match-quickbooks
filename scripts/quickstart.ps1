# ShipMatch quick start for Windows: no Docker, no API keys.
# Creates .venv, installs packages, builds the demo data and starts the server.
# Run from anywhere:
#   powershell -ExecutionPolicy Bypass -File scripts\quickstart.ps1
# Safe to run again: existing data is kept and duplicate PDFs are skipped.

$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

function Run([string]$exe, [string[]]$arguments) {
    & $exe @arguments
    if ($LASTEXITCODE -ne 0) { throw "Command failed: $exe $($arguments -join ' ')" }
}

# Find a real Python 3.10+. The Microsoft Store "python" shortcut exists on most PCs even when
# Python is not installed; it exits with an error, so every candidate is test-run before use.
function Find-Python {
    $probe = "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
    $candidates = @(
        @{ Exe = "py"; Args = @("-3") },
        @{ Exe = "python"; Args = @() },
        @{ Exe = "python3"; Args = @() }
    )
    foreach ($c in $candidates) {
        if (-not (Get-Command $c.Exe -ErrorAction SilentlyContinue)) { continue }
        $previous = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        try {
            $probeArgs = $c.Args + @("-c", $probe)
            & $c.Exe @probeArgs *> $null
            $ok = ($LASTEXITCODE -eq 0)
        } catch {
            $ok = $false
        } finally {
            $ErrorActionPreference = $previous
        }
        if ($ok) { return $c }
    }
    return $null
}

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    $python = Find-Python
    if (-not $python) {
        Write-Host ""
        Write-Host "Python 3.10 or newer is not installed (the 'python' command is only the Microsoft Store shortcut)." -ForegroundColor Yellow
        Write-Host "Install it, then CLOSE and REOPEN this terminal and run this script again:"
        Write-Host "  Option 1:  winget install -e --id Python.Python.3.12"
        Write-Host "  Option 2:  download Python 3.12 from https://www.python.org/downloads/windows/"
        Write-Host "             and tick 'Add python.exe to PATH' on the first installer screen."
        Write-Host "Check it worked with:  py --version"
        exit 1
    }
    Write-Host "Creating virtual environment .venv with '$($python.Exe) $($python.Args -join ' ')' ..."
    Run $python.Exe ($python.Args + @("-m", "venv", ".venv"))
}
$py = ".\.venv\Scripts\python.exe"
Run $py @("-c", "import sys; print('Using Python', sys.version.split()[0])")

Write-Host "Installing packages (first run takes a few minutes) ..."
Run $py @("-m", "pip", "install", "--quiet", "--upgrade", "pip")
Run $py @("-m", "pip", "install", "--quiet", "-r", "requirements.txt")

Run $py @("manage.py", "migrate", "-v", "0")
Run $py @("manage.py", "seed_demo")
if (-not (Test-Path "datasets\synthetic\ground_truth.json")) {
    Run $py @("manage.py", "generate_dataset", "--out", "datasets/synthetic", "--shipments", "20", "--seed", "42", "--scanned", "3")
}
Run $py @("manage.py", "ingest_folder", "datasets/synthetic", "--org", "demo")

Write-Host ""
Write-Host "Open http://localhost:8000/ and sign in. Demo users are listed in README.md (admin, reviewer, approver)." -ForegroundColor Green
Write-Host "Press Ctrl+C to stop the server." -ForegroundColor Green
Run $py @("manage.py", "runserver")
