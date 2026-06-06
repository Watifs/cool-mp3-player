; ============================================================================
;  Inno Setup script for Cool MP3 Player (Windows installer)
;
;  Why an installer (vs. shipping the bare .exe): this detects whether a copy is
;  already installed and UPGRADES IT IN PLACE instead of leaving the user with
;  two loose copies. That works because of the fixed AppId below — when Setup
;  runs and finds the same AppId in the registry, it installs over the existing
;  version (same folder), closing the running app first.
;
;  Build it with:  packaging\windows\build-installer.ps1
;  (which passes the version via /DMyAppVersion=… read from player.py)
; ============================================================================

#ifndef MyAppVersion
  #define MyAppVersion "1.0.0"
#endif

#define MyAppName "Cool MP3 Player"
#define MyAppExeName "Cool MP3 Player.exe"
#define MyAppPublisher "Watifs"
#define MyAppURL "https://github.com/Watifs/cool-mp3-player"
; Stable identity — DO NOT CHANGE between releases. This GUID is what lets a new
; installer recognise an older install and upgrade it rather than duplicate it.
#define MyAppId "{{8F3B6B1A-4C2E-4E7A-9E2A-1B7C9D2E5F30}"

[Setup]
AppId={#MyAppId}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}/releases/latest
; Per-user install — no UAC prompt, and upgrades cleanly under HKCU.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
OutputDir=..\..\dist\windows
OutputBaseFilename=Cool MP3 Player Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; Detect the running app and close it during an upgrade (Restart Manager).
CloseApplications=yes
RestartApplications=no
; Surfaced in Settings ▸ Apps / Add-Remove Programs.
UninstallDisplayName={#MyAppName} {#MyAppVersion}
UninstallDisplayIcon={app}\{#MyAppExeName}
VersionInfoVersion={#MyAppVersion}
VersionInfoCompany={#MyAppPublisher}
VersionInfoProductName={#MyAppName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional icons:"; Flags: unchecked

[Files]
Source: "..\..\dist\windows\Cool MP3 Player.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

[Code]
{ Compare two dotted numeric version strings.
  Returns -1 if A < B, 0 if equal, 1 if A > B. }
function CompareVersion(A, B: String): Integer;
var
  P, NA, NB: Integer;
begin
  Result := 0;
  while (Result = 0) and ((A <> '') or (B <> '')) do
  begin
    P := Pos('.', A);
    if P > 0 then begin NA := StrToIntDef(Copy(A, 1, P - 1), 0); Delete(A, 1, P); end
    else          begin NA := StrToIntDef(A, 0); A := ''; end;
    P := Pos('.', B);
    if P > 0 then begin NB := StrToIntDef(Copy(B, 1, P - 1), 0); Delete(B, 1, P); end
    else          begin NB := StrToIntDef(B, 0); B := ''; end;
    if NA < NB then Result := -1
    else if NA > NB then Result := 1;
  end;
end;

{ Read the version of any already-installed copy (per-user first, then machine).
  Empty string means nothing is installed. }
function InstalledVersion(): String;
var
  key: String;
begin
  Result := '';
  key := 'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\{#MyAppId}_is1';
  if not RegQueryStringValue(HKCU, key, 'DisplayVersion', Result) then
    if not RegQueryStringValue(HKLM, key, 'DisplayVersion', Result) then
      Result := '';
end;

function InitializeSetup(): Boolean;
var
  iv: String;
  cmp: Integer;
begin
  Result := True;
  iv := InstalledVersion();
  if iv = '' then
    exit;   { fresh install — nothing to compare }

  cmp := CompareVersion(iv, '{#MyAppVersion}');
  if cmp < 0 then
  begin
    { An older version is installed: this is the normal update path. Inno will
      install over it (same AppId/folder). Just let the user know. }
    MsgBox('Cool MP3 Player ' + iv + ' is already installed.' + #13#10 +
           'It will be updated to {#MyAppVersion}.',
           mbInformation, MB_OK);
  end
  else if cmp = 0 then
  begin
    if MsgBox('Cool MP3 Player {#MyAppVersion} is already installed.' + #13#10 +
              'Reinstall it?', mbConfirmation, MB_YESNO) = IDNO then
      Result := False;
  end
  else
  begin
    if MsgBox('A newer version (' + iv + ') is already installed.' + #13#10 +
              'Replace it with the older {#MyAppVersion} (downgrade)?',
              mbConfirmation, MB_YESNO) = IDNO then
      Result := False;
  end;
end;
