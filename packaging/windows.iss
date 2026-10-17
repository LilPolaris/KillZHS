; Compile through scripts/build_installer.py. Keep AppId stable across upgrades.
#ifndef AppVersion
  #error AppVersion must come from build metadata
#endif
#ifndef BundleDir
  #error BundleDir must be a validated onedir bundle
#endif
#ifndef ChineseLanguageFile
  #error ChineseLanguageFile must be the verified upstream translation
#endif

[Setup]
AppId={{6AF05399-6C56-44CD-9FA3-0BE83A516B4B}
AppName=KillZHS
AppVersion={#AppVersion}
AppPublisher=KillZHS
VersionInfoVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\KillZHS
DefaultGroupName=KillZHS
PrivilegesRequired=lowest
ArchitecturesAllowed=x64os
ArchitecturesInstallIn64BitMode=x64os
MinVersion=10.0
DisableDirPage=no
DisableProgramGroupPage=yes
UsePreviousTasks=no
UsePreviousLanguage=no
WizardStyle=modern
ShowLanguageDialog=no
Uninstallable=yes
UninstallDisplayIcon={app}\KillZHS.exe
CloseApplications=yes
CloseApplicationsFilter=*.exe,*.dll,*.pyd
RestartApplications=no
Compression=lzma2
SolidCompression=yes
OutputBaseFilename=KillZHS-{#AppVersion}-windows-x64-setup

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Default.isl,{#ChineseLanguageFile}"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式(&D)"; GroupDescription: "附加快捷方式："; Flags: unchecked

[Files]
Source: "{#BundleDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\KillZHS\KillZHS"; Filename: "{app}\KillZHS.exe"; WorkingDir: "{app}"
Name: "{userprograms}\KillZHS\卸载 KillZHS"; Filename: "{uninstallexe}"
Name: "{userdesktop}\KillZHS"; Filename: "{app}\KillZHS.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\KillZHS.exe"; Description: "启动 KillZHS(&L)"; WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent unchecked

; No broad cleanup, legacy migration, or userdata deletion. Inno's uninstall
; log owns only installed files and shortcuts. An unchecked desktop task does
; not touch a pre-existing shortcut (including the old portable app's link).
