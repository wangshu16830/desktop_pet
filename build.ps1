# Run from the project directory: powershell -ExecutionPolicy Bypass -File .\build.ps1
# Output: release\DesktopPet-clean. Distribute that entire folder.
$ErrorActionPreference = 'Stop'
$projectDir = $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($projectDir)) {
    $projectDir = (Get-Location).Path
}
Set-Location $projectDir

$python = Join-Path $projectDir '.venv-build\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw 'Missing .venv-build. Create it and install requirements first.'
}

# NumPy's OpenBLAS DLL must be placed in the _internal root DLL search path.
# PyInstaller 6.6 does not collect this DLL automatically.
$numpyLib = Get-ChildItem -LiteralPath (Join-Path $projectDir '.venv-build\Lib\site-packages\numpy.libs') -Filter 'libopenblas*.dll' | Select-Object -First 1
if ($null -eq $numpyLib) {
    throw 'NumPy OpenBLAS DLL was not found in .venv-build.'
}

& $python -m PyInstaller --noconfirm --clean --windowed --name DesktopPet --add-binary "$($numpyLib.FullName);." desktop_pet.py
if ($LASTEXITCODE -ne 0) {
    throw 'PyInstaller build failed. Run: python -m pip install -r requirements.txt'
}

$releaseDir = Join-Path $projectDir 'release\DesktopPet-clean'
if (Test-Path -LiteralPath $releaseDir) {
    Remove-Item -LiteralPath $releaseDir -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $releaseDir | Out-Null
# PyInstaller creates a one-directory app; include its _internal runtime folder.
Copy-Item -Path '.\dist\DesktopPet\*' -Destination $releaseDir -Recurse -Force
Copy-Item -LiteralPath '.\pet.json' -Destination $releaseDir -Force

$config = Get-Content -LiteralPath '.\pet.json' -Raw -Encoding UTF8 | ConvertFrom-Json
$assetSource = Join-Path $projectDir $config.video_directory
if (-not (Test-Path -LiteralPath $assetSource)) {
    throw "Asset directory not found: $assetSource"
}
if ($config.video_directory -eq '.') {
    Get-ChildItem -LiteralPath $assetSource -Filter '*.mp4' | Copy-Item -Destination $releaseDir -Force
} else {
    Copy-Item -LiteralPath $assetSource -Destination (Join-Path $releaseDir $config.video_directory) -Recurse -Force
}

Write-Host "Build complete: $releaseDir"
