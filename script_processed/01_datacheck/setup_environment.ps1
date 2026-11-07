$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$requirements = Join-Path $projectRoot "script_processed\01_datacheck\requirements.txt"
$venvDir = Join-Path $projectRoot ".venv"
$venvPython = Join-Path $venvDir "Scripts\python.exe"

if (-not (Test-Path -LiteralPath $venvPython)) {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pythonCommand) { $pythonCommand = Get-Command py -ErrorAction SilentlyContinue }
    if (-not $pythonCommand) { throw "No Python interpreter found on PATH." }
    Write-Host "[INFO] Creating project-local virtual environment: $venvDir"
    & $pythonCommand.Source -m venv $venvDir
    if ($LASTEXITCODE -ne 0) { throw "Could not create .venv." }
}

Write-Host "[INFO] Project Python: $venvPython"
& $venvPython -c "import sys; print('[INFO] Version:', sys.version.split()[0]); print('[INFO] Executable:', sys.executable)"
$pipArgs = @("-m", "pip", "install", "-r", $requirements)
& $venvPython @pipArgs
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }

& $venvPython -c "import xarray, netCDF4, rasterio; from pyhdf import SD; print('[ OK ] project datacheck environment ready'); print('       xarray', xarray.__version__); print('       netCDF4', netCDF4.__version__); print('       rasterio', rasterio.__version__); print('       pyhdf ready')"
if ($LASTEXITCODE -ne 0) { throw "Readers could not be imported." }
Write-Host "[ OK ] Use .\.venv\Scripts\python.exe to run project scripts."
