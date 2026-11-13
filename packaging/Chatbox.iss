#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppName=Chatbox
AppVersion={#AppVersion}
AppPublisher=Chatbox
DefaultDirName={autopf}\Chatbox
DefaultGroupName=Chatbox
OutputDir=..\dist\artifacts
OutputBaseFilename=Chatbox-Windows-x64-setup
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
PrivilegesRequired=lowest
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\Chatbox.exe

[Files]
Source: "..\dist\Chatbox\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Chatbox"; Filename: "{app}\Chatbox.exe"
Name: "{autodesktop}\Chatbox"; Filename: "{app}\Chatbox.exe"

[Run]
Filename: "{app}\Chatbox.exe"; Description: "Launch Chatbox"; Flags: nowait postinstall skipifsilent
