param([switch]$Stop)
$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$projectRoot = Split-Path $PSScriptRoot -Parent
$backendDir = Join-Path $projectRoot 'backend'
$frontendDir = Join-Path $projectRoot 'frontend'
$runtimeDir = Join-Path $backendDir 'runtime'
$processFile = Join-Path $runtimeDir 'classroom-processes.json'
$pythonExe = Join-Path $backendDir '.venv\Scripts\python.exe'

function Test-ClassroomProcess($entry) {
    if (-not $entry -or -not $entry.processId -or -not $entry.executable -or -not $entry.marker) { return $false }
    $running = Get-CimInstance Win32_Process -Filter "ProcessId=$($entry.processId)" -ErrorAction SilentlyContinue
    return [bool]($running -and $running.ExecutablePath -eq $entry.executable -and
        $running.CommandLine -and $running.CommandLine.Contains($entry.marker))
}

function Test-ClassroomListener($entry, $ownerId) {
    if (-not (Test-ClassroomProcess $entry)) { return $false }
    # Windows venv python.exe can be a launcher; its child owns the API port.
    $candidateId = [int]$ownerId
    for ($depth = 0; $depth -lt 8 -and $candidateId -gt 0; $depth++) {
        if ($candidateId -eq [int]$entry.processId) { return $true }
        $candidate = Get-CimInstance Win32_Process -Filter "ProcessId=$candidateId" -ErrorAction SilentlyContinue
        if (-not $candidate) { return $false }
        $candidateId = [int]$candidate.ParentProcessId
    }
    return $false
}

function Test-ClassroomReady {
    try {
        $apiReply = Invoke-RestMethod 'http://127.0.0.1:8787/api/health' -TimeoutSec 2
        $webReply = Invoke-WebRequest 'http://127.0.0.1:5173/retrieval' -UseBasicParsing -TimeoutSec 2
        return ($apiReply.database -eq 'healthy' -and $webReply.StatusCode -eq 200)
    } catch { return $false }
}

if ($Stop) {
    if (Test-Path -LiteralPath $processFile) {
        foreach ($entry in (Get-Content -LiteralPath $processFile -Raw | ConvertFrom-Json)) {
            if (Test-ClassroomProcess $entry) {
                & "$env:SystemRoot\System32\taskkill.exe" /PID $entry.processId /T /F | Out-Null
            }
        }
        Remove-Item -LiteralPath $processFile
    }
    Write-Output 'Classroom services stopped.'
    exit 0
}

Push-Location $backendDir
try {
    $status = (& $pythonExe 'scripts/classroom_local.py' 'status' | ConvertFrom-Json)
    if ($LASTEXITCODE -ne 0 -or $status.database -ne 'yuan_classroom_local' -or
        -not $status.oss_configured -or -not $status.paid_calls_enabled) {
        throw 'Initialize yuan_classroom_local and configure OSS before starting.'
    }
    & $pythonExe 'scripts/check_classroom_schema.py'
    if ($LASTEXITCODE -ne 0) { throw 'Database schema check failed; see migration instructions above.' }
} finally { Pop-Location }

$listeners = [System.Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners()
if ($listeners | Where-Object { $_.Port -in @(5173, 8787) }) {
    $ownedAndReady = $false
    if (Test-Path -LiteralPath $processFile) {
        try {
            # Windows PowerShell 5.1 returns the JSON array as one pipeline object.
            # Assign directly so the following pipeline enumerates its entries.
            $recorded = Get-Content -LiteralPath $processFile -Raw | ConvertFrom-Json
            $apiEntry = @($recorded | Where-Object { $_.name -eq 'api' })
            $webEntry = @($recorded | Where-Object { $_.name -eq 'frontend' })
            $workerEntry = @($recorded | Where-Object { $_.name -eq 'worker' })
            if ($apiEntry.Count -eq 1 -and $webEntry.Count -eq 1 -and $workerEntry.Count -eq 1) {
                $connections = @(Get-NetTCPConnection -State Listen -ErrorAction Stop)
                $apiOwners = @($connections | Where-Object { $_.LocalPort -eq 8787 })
                $webOwners = @($connections | Where-Object { $_.LocalPort -eq 5173 })
                $apiOwned = $apiOwners.Count -gt 0
                foreach ($connection in $apiOwners) {
                    if (-not (Test-ClassroomListener $apiEntry[0] $connection.OwningProcess)) { $apiOwned = $false }
                }
                $webOwned = $webOwners.Count -gt 0
                foreach ($connection in $webOwners) {
                    if (-not (Test-ClassroomListener $webEntry[0] $connection.OwningProcess)) { $webOwned = $false }
                }
                $ownedAndReady = $apiOwned -and $webOwned -and (Test-ClassroomProcess $workerEntry[0]) -and (Test-ClassroomReady)
            }
        } catch {
            Write-Warning "Could not verify recorded services: $($_.Exception.Message)"
            $ownedAndReady = $false
        }
    }
    if ($ownedAndReady) {
        Write-Output 'Classroom is already running: http://localhost:5173/'
        exit 0
    }
    throw 'Port 5173 or 8787 is occupied, but a complete recorded classroom service could not be verified. No services were stopped. For this project, run .\scripts\start-classroom.ps1 -Stop before starting again; check other port owners separately.'
}
$nodeCmd = Get-Command node -ErrorAction SilentlyContinue
$nodeExe = if ($nodeCmd) { $nodeCmd.Source } else {
    Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe'
}
$nodeExe = [System.IO.Path]::GetFullPath($nodeExe)
New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null
$launched = @()
function Start-ClassroomProcess($name, $executable, $arguments, $directory, $marker) {
    $task = Start-Process -FilePath $executable -ArgumentList $arguments -WorkingDirectory $directory `
        -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runtimeDir "$name.stdout.log") `
        -RedirectStandardError (Join-Path $runtimeDir "$name.stderr.log") -PassThru
    return [pscustomobject]@{ processId=$task.Id; executable=$executable; marker=$marker; name=$name }
}
try {
    $launched += Start-ClassroomProcess 'api' $pythonExe @('-m','uvicorn','app.main:app','--host','127.0.0.1','--port','8787') $backendDir 'app.main:app'
    $launched += Start-ClassroomProcess 'worker' $pythonExe @('-m','app.classroom_worker') $backendDir 'app.classroom_worker'
    $viteFile = Join-Path $frontendDir 'node_modules\vite\bin\vite.js'
    $launched += Start-ClassroomProcess 'frontend' $nodeExe @("`"$viteFile`"",'--host','127.0.0.1','--port','5173','--strictPort') $frontendDir 'vite.js'
    $launched | ConvertTo-Json | Set-Content -LiteralPath $processFile -Encoding utf8
    $ready = $false
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        foreach ($entry in $launched) {
            if (-not (Test-ClassroomProcess $entry)) {
                throw "Startup failed: $($entry.name) exited; check backend/runtime/$($entry.name).stderr.log."
            }
        }
        $workerReady = Select-String -LiteralPath (Join-Path $runtimeDir 'worker.stderr.log') -Pattern 'Classroom worker ready' -Quiet
        if ($workerReady -and (Test-ClassroomReady)) { $ready = $true; break }
        Start-Sleep -Milliseconds 500
    }
    if (-not $ready) { throw 'Startup failed; check backend/runtime/*.stderr.log.' }
    Write-Output 'Classroom ready: http://localhost:5173/'
} catch {
    foreach ($entry in $launched) {
        if (Test-ClassroomProcess $entry) {
            & "$env:SystemRoot\System32\taskkill.exe" /PID $entry.processId /T /F | Out-Null
        }
    }
    Remove-Item -LiteralPath $processFile -ErrorAction SilentlyContinue
    throw
}
