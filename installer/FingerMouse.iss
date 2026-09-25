; Finger Mouse — Windows installer (Inno Setup 6.3 or newer).
;
; Build the app first (python -m PyInstaller FingerMouse.spec), then:
;
;   iscc /DMyAppVersion=2.2.0 installer\FingerMouse.iss
;
; Optional defines:
;   /DDistDir=<folder>     the PyInstaller output folder (default ..\dist\FingerMouse)
;   /DOutputDir=<folder>   where the setup .exe goes    (default ..\dist)
;   /DSIGN "/Sfmsign=<signing command with $f>"
;                          sign the installer and its uninstaller as they're built;
;                          see RELEASING.md. Without it, both are unsigned.
;
; Installs per user (no administrator prompt) into %LOCALAPPDATA%\Programs,
; or for everyone if the person chooses that and can approve it.

#ifndef MyAppVersion
  #define MyAppVersion "2.2.0"
#endif
#ifndef DistDir
  #define DistDir "..\dist\FingerMouse"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist"
#endif
#define MyAppName "Finger Mouse"
#define MyAppPublisher "Code-A-Difference"
#define MyAppURL "https://codeadifference.ct.ws/projects/finger-tracking-mouse/"
#define MyAppSource "https://github.com/Code-A-Difference/Finger-Tracking-Mouse"
#define MyAppExeName "FingerMouse.exe"

[Setup]
; The AppId identifies Finger Mouse to Windows across versions: never change it.
AppId={{8D8B48CB-3E9D-4B1F-8A25-19FE6DA9E95B}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppSource}/issues
AppUpdatesURL={#MyAppURL}
AppCopyright=© {#MyAppPublisher}. MIT License.
VersionInfoVersion={#MyAppVersion}
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription={#MyAppName} Setup
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#MyAppVersion}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#OutputDir}
OutputBaseFilename=FingerMouse-windows-x64-setup
SetupIconFile=..\assets\FingerMouse.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
LicenseFile=..\LICENSE
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; An update closes a running Finger Mouse first, so files can be replaced.
CloseApplications=yes
RestartApplications=no
#ifdef SIGN
SignTool=fmsign
SignedUninstaller=yes
#endif

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[InstallDelete]
; Files from an older version that the new one no longer ships.
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Comment: "Control the pointer with your hand"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent

; Settings live in %APPDATA%\Finger Mouse and are kept on uninstall, so a
; reinstall remembers them.
