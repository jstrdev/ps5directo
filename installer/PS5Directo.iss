; Instalador de PS5 Directo (Inno Setup 6). Lo compila GitHub Actions en cada version:
;   ISCC /DMyVer=X.Y.Z installer\PS5Directo.iss
; Espera la carpeta del programa ya montada (con Python incluido) en dist\PS5Directo
; y deja dist\PS5Directo-X.Y.Z-Setup.exe.
; Se instala para el usuario actual (sin pedir permisos de administrador), en
; %LOCALAPPDATA%\Programs\PS5 Directo, para que «Actualizar» pueda escribir en la carpeta.

#ifndef MyVer
  #define MyVer "0.0.0"
#endif
#ifndef SrcDir
  #define SrcDir "..\dist\PS5Directo"
#endif
#define Launch """{app}\PS5 Directo.vbs"""

[Setup]
AppId={{BD789BF5-0254-4A4C-92CB-332BB22CEE42}
AppName=PS5 Directo
AppVersion={#MyVer}
AppVerName=PS5 Directo {#MyVer}
AppPublisher=PS5 Directo
AppPublisherURL=https://github.com/jstrdev/ps5directo
AppSupportURL=https://github.com/jstrdev/ps5directo/issues
AppUpdatesURL=https://github.com/jstrdev/ps5directo/releases
DefaultDirName={localappdata}\Programs\PS5 Directo
DefaultGroupName=PS5 Directo
DisableProgramGroupPage=yes
DisableReadyPage=no
DisableDirPage=yes
UsePreviousAppDir=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\dist
OutputBaseFilename=PS5Directo-{#MyVer}-Setup
SetupIconFile={#SrcDir}\web\ps5directo.ico
WizardSmallImageFile=small.bmp,small2x.bmp
UninstallDisplayIcon={app}\web\ps5directo.ico
UninstallDisplayName=PS5 Directo
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
ShowLanguageDialog=no
LanguageDetectionMethod=uilanguage

[Languages]
Name: "es"; MessagesFile: "compiler:Languages\Spanish.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "{#SrcDir}\*"; DestDir: "{app}"; Excludes: "Crear accesos directos.bat,__pycache__"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; restos de una version anterior que ya no se usan
Type: files; Name: "{app}\Crear accesos directos.bat"

[Icons]
Name: "{autoprograms}\PS5 Directo"; Filename: "{sys}\wscript.exe"; Parameters: {#Launch}; WorkingDir: "{app}"; IconFilename: "{app}\web\ps5directo.ico"; Comment: "PS5 Directo"
Name: "{autodesktop}\PS5 Directo"; Filename: "{sys}\wscript.exe"; Parameters: {#Launch}; WorkingDir: "{app}"; IconFilename: "{app}\web\ps5directo.ico"; Comment: "PS5 Directo"
Name: "{usersendto}\PS5 Directo"; Filename: "{sys}\wscript.exe"; Parameters: {#Launch}; WorkingDir: "{app}"; IconFilename: "{app}\web\ps5directo.ico"; Comment: "PS5 Directo"

[Run]
Filename: "{sys}\wscript.exe"; Parameters: {#Launch}; WorkingDir: "{app}"; Description: "{cm:LaunchProgram,PS5 Directo}"; Flags: postinstall nowait skipifsilent

[UninstallRun]
; cierra PS5 Directo si sigue abierto, para poder borrar sus archivos
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -Command ""Get-Process python,pythonw -ErrorAction SilentlyContinue | Where-Object {{ $_.Path -like '{app}\*' } | Stop-Process -Force"""; Flags: runhidden; RunOnceId: "CerrarPS5Directo"

[UninstallDelete]
; tambien lo que añadio «Actualizar» despues de instalar. Los ajustes del usuario
; (carpeta .ps5directo) no se tocan.
Type: filesandordirs; Name: "{app}"
