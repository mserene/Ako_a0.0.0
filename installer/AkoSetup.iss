#define MyAppName "Ako"
#define MyAppVersion "0.1.3"
#define MyAppPublisher "mserene"
#define MyAppExeName "Ako-ai.exe"
#define MyAppLauncherName "Ako-ai_launcher.vbs"
#define PythonEmbedZip "python-3.12.10-embed-amd64.zip"

[Setup]
AppId={{5E2E5C71-35B1-4629-8C4F-71A987F07B90}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\Ako
DefaultGroupName=Ako
DisableProgramGroupPage=yes
OutputDir=..\installer_output
OutputBaseFilename=AkoSetup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"

[Tasks]
Name: "desktopicon"; Description: "바탕화면 바로가기 만들기"; GroupDescription: "추가 옵션:"; Flags: unchecked

[Files]
Source: "..\dist\Ako-ai\*"; DestDir: "{app}"; Excludes: "*.log,memory_state.json,assistant_prefs.json,Ako-ai_launcher.bat,requirements.txt,debug_ocr\*"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\bootstrap_runtime.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\Ako-ai_launcher.vbs"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\installer_assets\{#PythonEmbedZip}"; DestDir: "{app}\runtime_assets"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\Ako"; Filename: "{sys}\wscript.exe"; Parameters: """{app}\{#MyAppLauncherName}"""; WorkingDir: "{app}"; IconFilename: "{app}\{#MyAppExeName}"; IconIndex: 0
Name: "{autodesktop}\Ako"; Filename: "{sys}\wscript.exe"; Parameters: """{app}\{#MyAppLauncherName}"""; WorkingDir: "{app}"; IconFilename: "{app}\{#MyAppExeName}"; IconIndex: 0; Tasks: desktopicon

[Run]
Filename: "{sys}\wscript.exe"; Parameters: """{app}\{#MyAppLauncherName}"""; Description: "Ako 실행하기"; Flags: nowait postinstall skipifsilent
