# Starts the full local-dev backend stack (docker compose: moto, postgres,
# redis, the one-shot bootstrap container, and all ten app services) and
# verifies every service actually responds - not just that its container
# shows "Up". Safe to re-run any time; docker compose reuses/recreates
# containers as needed and bootstrap re-seeds idempotently.
#
# Usage (from anywhere):
#   powershell -ExecutionPolicy Bypass -File services\local-dev\start-backend.ps1

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "==> Bringing up docker compose stack..."
docker compose up -d

Write-Host "==> Waiting for postgres and moto healthchecks..."
foreach ($name in @("local-dev-postgres-1", "local-dev-moto-1")) {
    $status = "unknown"
    for ($i = 0; $i -lt 30; $i++) {
        try {
            $status = (docker inspect --format '{{.State.Health.Status}}' $name 2>$null)
            if (-not $status) { $status = "unknown" }
        } catch {
            $status = "unknown"
        }
        if ($status -eq "healthy") { break }
        Start-Sleep -Seconds 2
    }
    Write-Host "   $name -> $status"
}

Write-Host "==> Waiting for bootstrap (migrations + seeds) to finish..."
docker wait local-dev-bootstrap-1 | Out-Null

Write-Host ""
Write-Host "==> Service health check:"
$services = [ordered]@{
    "identity-auth" = 8001
    "user"          = 8002
    "inventory"     = 8000
    "catalog"       = 8003
    "cart"          = 8004
    "pricing-offer" = 8005
    "wallet"        = 8006
    "payment"       = 8007
    "subscription"  = 8008
    "order"         = 8009
}

$allOk = $true
foreach ($name in $services.Keys) {
    $port = $services[$name]
    $code = $null
    # App containers have no docker healthcheck, so "Started" doesn't mean
    # "accepting connections yet" - retry a few times before calling it
    # unreachable, rather than a single-shot check racing container startup.
    for ($attempt = 0; $attempt -lt 5; $attempt++) {
        try {
            $resp = Invoke-WebRequest -Uri "http://localhost:$port/" -UseBasicParsing -TimeoutSec 5 -ErrorAction Stop
            $code = [int]$resp.StatusCode
        } catch {
            if ($_.Exception.Response) {
                $code = [int]$_.Exception.Response.StatusCode
            } else {
                $code = $null
            }
        }
        if ($null -ne $code) { break }
        Start-Sleep -Seconds 2
    }
    if ($null -eq $code) {
        Write-Host ("   {0,-15}:{1,-5} -> UNREACHABLE" -f $name, $port)
        $allOk = $false
    } else {
        Write-Host ("   {0,-15}:{1,-5} -> {2} (up)" -f $name, $port, $code)
    }
}

Write-Host ""
if ($allOk) {
    Write-Host "All services responding. Backend is ready."
    Write-Host "See services/local-dev/README.md for how to exercise each API, and"
    Write-Host "milkful-app/README.md's 'Running on an Android emulator' section if"
    Write-Host "you're pointing the Flutter app at this stack from an emulator."
} else {
    Write-Host "One or more services are unreachable." -ForegroundColor Red
    Write-Host "Investigate with: docker compose logs <service-name>" -ForegroundColor Red
    exit 1
}
