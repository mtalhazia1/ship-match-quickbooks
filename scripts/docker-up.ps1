# Start ShipMatch in Docker Desktop: Postgres, Redis, the web app and background workers.
# Usage (PowerShell, from the shipmatch folder):
#   powershell -ExecutionPolicy Bypass -File scripts\docker-up.ps1
# Stop:  docker compose down        (your data stays in the "pgdata" volume)
# Wipe:  docker compose down -v     (deletes the Docker database)
# "Continue": Docker writes progress to stderr, which Windows PowerShell would otherwise treat as an error.
$ErrorActionPreference = "Continue"
Set-Location (Split-Path $PSScriptRoot -Parent)

function Fail($msg) { Write-Host $msg -ForegroundColor Red; exit 1 }

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Fail "Docker Desktop is not installed. Get it from https://www.docker.com/products/docker-desktop/, restart Windows, then run this again."
}
docker info *> $null
$dockerOk = ($LASTEXITCODE -eq 0)
if (-not $dockerOk) {
    Fail "Docker Desktop is not running. Start it from the Start menu, wait until it shows 'Engine running', then run this again."
}
$busy = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
if ($busy) {
    $owner = (Get-Process -Id $busy[0].OwningProcess -ErrorAction SilentlyContinue).ProcessName
    if ($owner -notmatch "docker|com.docker|wslrelay|vpnkit") {
        Fail "Port 8000 is in use by '$owner' (probably 'manage.py runserver'). Stop it with Ctrl+C in its window, then run this again."
    }
}
if (-not (Test-Path ".env")) {
    Write-Host "No .env file: ShipMatch will start without QuickBooks or Claude. See README.md to add keys." -ForegroundColor Yellow
}

Write-Host "Building and starting containers (the first run downloads images and takes a few minutes) ..."
docker compose up -d --build
if ($LASTEXITCODE -ne 0) { Fail "docker compose failed. Scroll up for the first error." }

Write-Host "Waiting for the web app ..."
$healthy = $false
$webId = (docker compose ps -q web | Select-Object -First 1)
for ($i = 0; $i -lt 60; $i++) {
    $state = docker inspect -f "{{.State.Health.Status}}" $webId 2>$null
    if ($state -eq "healthy") { $healthy = $true; break }
    Start-Sleep -Seconds 5
}
if (-not $healthy) { Fail "The web app did not start. Run 'docker compose logs web' to see why." }

docker compose exec -T web python manage.py seed_demo
if (-not (Test-Path "datasets\synthetic\ground_truth.json")) {
    docker compose exec -T web python manage.py generate_dataset --out datasets/synthetic --shipments 20 --seed 42 --scanned 3
}
docker compose exec -T web python manage.py ingest_folder datasets/synthetic --org demo --async

Write-Host ""
Write-Host "ShipMatch is running at http://localhost:8000/ (Postgres, Redis and 2 background workers)." -ForegroundColor Green
Write-Host "Demo users are listed in README.md. Logs: docker compose logs -f web worker"
Write-Host "Pilot with your own PDFs: docker compose exec web python manage.py pilot"
