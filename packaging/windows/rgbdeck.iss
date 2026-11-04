; Inno Setup script for RGB Deck. Built on CI:  ISCC.exe /DAppVersion=1.2.0 packaging\windows\rgbdeck.iss
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{A3D4B6E1-7C2F-4F0A-9E5B-2D1C8F6A7B90}
AppName=RGB Deck
AppVersion={#AppVersion}
AppVerName=RGB Deck {#AppVersion}
AppPublisher=dancingduck425
AppPublisherURL=https://github.com/dancingduck425/rgbdeck
AppSupportURL=https://github.com/dancingduck425/rgbdeck/issues
DefaultDirName={autopf}\RGB Deck
DefaultGroupName=RGB Deck
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\..\dist
OutputBaseFilename=RGBDeck-Setup-{#AppVersion}
SetupIconFile=..\..\rgbdeck\data\rgbdeck.ico
UninstallDisplayIcon={app}\RGBDeck.exe
LicenseFile=..\..\LICENSE
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; Flags: unchecked
Name: "autostart"; Description: "Start RGB Deck in the tray when I log in"

[Files]
Source: "..\..\dist\RGBDeck.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\RGB Deck"; Filename: "{app}\RGBDeck.exe"
Name: "{autodesktop}\RGB Deck"; Filename: "{app}\RGBDeck.exe"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "RGB Deck"; ValueData: """{app}\RGBDeck.exe"" --tray"; Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\RGBDeck.exe"; Description: "Launch RGB Deck"; Flags: nowait postinstall skipifsilent
