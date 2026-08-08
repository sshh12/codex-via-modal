$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$callerDirectory = (Get-Location).Path
$venvDirectory = Join-Path $scriptRoot ".venv"
$venvPython = Join-Path $venvDirectory "Scripts\python.exe"

if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    $bootstrap = Get-Command py -ErrorAction SilentlyContinue
    $bootstrapArguments = @("-3", "-m", "venv", $venvDirectory)
    if ($null -eq $bootstrap) {
        $bootstrap = Get-Command python3 -ErrorAction SilentlyContinue
        $bootstrapArguments = @("-m", "venv", $venvDirectory)
    }
    if ($null -eq $bootstrap) {
        $bootstrap = Get-Command python -ErrorAction SilentlyContinue
    }
    if ($null -eq $bootstrap) {
        throw "Python 3.10 or newer is required (tried py, python3, and python)."
    }

    Write-Host "Creating codex-modal's local Python environment..."
    $savedPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & $bootstrap.Source @bootstrapArguments
    $bootstrapExitCode = $LASTEXITCODE
    $ErrorActionPreference = $savedPreference
    if ($bootstrapExitCode -ne 0 -or -not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        throw "Could not create the local Python environment (exit $bootstrapExitCode)."
    }
}

$previousCaller = [Environment]::GetEnvironmentVariable("CODEX_MODAL_CALLER_CWD", "Process")
$previousPythonUtf8 = [Environment]::GetEnvironmentVariable("PYTHONUTF8", "Process")
$previousPythonIoEncoding = [Environment]::GetEnvironmentVariable("PYTHONIOENCODING", "Process")
[Environment]::SetEnvironmentVariable("CODEX_MODAL_CALLER_CWD", $callerDirectory, "Process")
[Environment]::SetEnvironmentVariable("PYTHONUTF8", "1", "Process")
[Environment]::SetEnvironmentVariable("PYTHONIOENCODING", "utf-8", "Process")
$exitCode = 1
Push-Location -LiteralPath $scriptRoot
try {
    # Windows PowerShell can turn native stderr into NativeCommandError under Stop.
    $savedPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & $venvPython -m codex_modal @args
    $exitCode = $LASTEXITCODE
    $ErrorActionPreference = $savedPreference
}
finally {
    Pop-Location
    [Environment]::SetEnvironmentVariable(
        "CODEX_MODAL_CALLER_CWD", $previousCaller, "Process"
    )
    [Environment]::SetEnvironmentVariable("PYTHONUTF8", $previousPythonUtf8, "Process")
    [Environment]::SetEnvironmentVariable(
        "PYTHONIOENCODING", $previousPythonIoEncoding, "Process"
    )
}
exit $exitCode
