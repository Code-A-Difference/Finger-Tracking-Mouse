<#
  Build the Windows installer, signing everything when a certificate is available.

    packaging\windows\build_installer.ps1 -Version 2.2.0

  Expects the PyInstaller app in dist\FingerMouse. Produces
  dist\FingerMouse-windows-x64-setup.exe.

  Signing (optional; see RELEASING.md). Values come from CI secrets:
    WINDOWS_CERT_PFX_BASE64   an Authenticode code-signing certificate (.pfx), base64
    WINDOWS_CERT_PASSWORD     its password
  The certificate is imported into the current user's store for the length
  of the build and removed afterwards; the .pfx never touches the disk after
  import, and the password never appears on a command line. FingerMouse.exe,
  the installer and its uninstaller are all signed with an RFC 3161
  timestamp, so the signatures stay valid after the certificate expires.

  Certificates issued since June 2023 live on hardware or a cloud HSM and
  can't be exported as .pfx; for those, sign with your provider's tool
  (Azure Trusted Signing is wired into the workflow) and leave these unset.
#>
param(
  [Parameter(Mandatory = $true)][string]$Version,
  [string]$DistDir = "",          # default: dist\ in the repository
  [string]$TimestampUrl = "http://timestamp.digicert.com"
)
$ErrorActionPreference = "Stop"
$root = (Resolve-Path "$PSScriptRoot\..\..").Path
$dist = if ($DistDir) { $DistDir } else { Join-Path $root "dist" }
$defines = @("/DMyAppVersion=$Version", "/DDistDir=$dist\FingerMouse", "/DOutputDir=$dist")

function Find-Tool($name, $globs) {
  $cmd = Get-Command $name -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }
  foreach ($glob in $globs) {
    $hit = Get-ChildItem $glob -ErrorAction SilentlyContinue | Sort-Object FullName -Descending | Select-Object -First 1
    if ($hit) { return $hit.FullName }
  }
  throw "$name not found. Install it first."
}

$iscc = Find-Tool "ISCC.exe" @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
                               "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe")
$thumbprint = $null
try {
  if ($env:WINDOWS_CERT_PFX_BASE64) {
    $signtool = Find-Tool "signtool.exe" @("${env:ProgramFiles(x86)}\Windows Kits\10\bin\*\x64\signtool.exe")
    $bytes = [Convert]::FromBase64String($env:WINDOWS_CERT_PFX_BASE64)
    $cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2(
      $bytes, $env:WINDOWS_CERT_PASSWORD,
      [System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]"UserKeySet,PersistKeySet")
    $store = New-Object System.Security.Cryptography.X509Certificates.X509Store("My", "CurrentUser")
    $store.Open("ReadWrite"); $store.Add($cert); $store.Close()
    $thumbprint = $cert.Thumbprint
    Write-Host "Signing as: $($cert.Subject)"
    # Inno Setup runs this for the installer and the uninstaller. Its own
    # placeholders ($q = a quote, $f = the file) avoid nested quotes, which
    # Windows PowerShell 5 mangles when passing arguments to programs.
    $sign = "`$q$signtool`$q sign /sha1 $thumbprint /fd sha256 /tr $TimestampUrl /td sha256 /d `$qFinger Mouse`$q `$f"

    & $signtool sign /sha1 $thumbprint /fd sha256 /tr $TimestampUrl /td sha256 /d "Finger Mouse" "$dist\FingerMouse\FingerMouse.exe"
    if ($LASTEXITCODE) { throw "signing FingerMouse.exe failed" }
    & $iscc /Q @defines /DSIGN "/Sfmsign=$sign" "$root\installer\FingerMouse.iss"
  } else {
    Write-Host "No certificate configured: building an unsigned installer."
    & $iscc /Q @defines "$root\installer\FingerMouse.iss"
  }
  if ($LASTEXITCODE) { throw "Inno Setup failed" }
} finally {
  if ($thumbprint) {
    Get-ChildItem "Cert:\CurrentUser\My\$thumbprint" -ErrorAction SilentlyContinue | ForEach-Object {
      $store = New-Object System.Security.Cryptography.X509Certificates.X509Store("My", "CurrentUser")
      $store.Open("ReadWrite"); $store.Remove($_); $store.Close()
    }
  }
}
$setup = Join-Path $dist "FingerMouse-windows-x64-setup.exe"
$sig = Get-AuthenticodeSignature $setup
Write-Host "Built $setup (signature: $($sig.Status))"
