; Build with: iscc /DMyAppVersion=2.2.0 installer\FingerMouse.iss
; The signing step is intentionally in CI, after this installer is produced.
#ifndef MyAppVersion
  #define MyAppVersion "2.2.0"
#endif
#define MyAppName "Finger Mouse"
#define MyAppPublisher "Code-A-Difference"
#define MyAppExeName "FingerMouse.exe"

[Setup]
AppId={{8D8B48CB-3E9D-4B1F-8A25-19FE6DA9E95B}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\Finger Mouse
DefaultGroupName=Finger Mouse
UninstallDisplayName=Finger Mouse
OutputDir=dist
OutputBaseFilename=FingerMouse-windows-x64-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest

[Files]
Source: "dist\FingerMouse\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{autoprograms}\Finger Mouse"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\Finger Mouse"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch Finger Mouse"; Flags: nowait postinstall skipifsilent
