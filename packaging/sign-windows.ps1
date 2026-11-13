# Sign Windows binaries with a PFX when WINDOWS_CERT_PFX_BASE64 is set.
# Azure Trusted Signing is a separate workflow step. With neither secret, this exits 0.
param(
    [Parameter(Mandatory = $true)][string]$Folder
)
$ErrorActionPreference = "Stop"
if (-not $env:WINDOWS_CERT_PFX_BASE64 -or -not $env:WINDOWS_CERT_PASSWORD) {
    Write-Host "No Windows PFX secrets. Leaving binaries unsigned."
    exit 0
}
if (-not (Test-Path $Folder)) {
    Write-Error "Missing $Folder"
    exit 1
}
$pfx = Join-Path $env:TEMP "chatbox-sign.pfx"
[IO.File]::WriteAllBytes($pfx, [Convert]::FromBase64String($env:WINDOWS_CERT_PFX_BASE64))
$kits = Get-ChildItem "C:\Program Files (x86)\Windows Kits\10\bin" -Recurse -Filter signtool.exe -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -match "\\x64\\signtool.exe$" } |
    Sort-Object FullName -Descending |
    Select-Object -First 1
if (-not $kits) { Write-Error "signtool.exe was not found"; exit 1 }
$files = Get-ChildItem $Folder -Recurse -Include *.exe
if (-not $files) { Write-Host "No executables in $Folder"; exit 0 }
foreach ($file in $files) {
    & $kits.FullName sign /fd SHA256 /td SHA256 /tr http://timestamp.digicert.com /f $pfx /p $env:WINDOWS_CERT_PASSWORD $file.FullName
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
Remove-Item $pfx -Force
Write-Host "Signed $($files.Count) file(s) in $Folder"
