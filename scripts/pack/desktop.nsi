; Lantai Desktop NSIS installer. Run makensis from repo root after
; building dist/win-unpacked (see scripts/pack/build_win.ps1).
; Usage: makensis /DLANTAI_VERSION=1.2.3 /DOUTPUT_EXE=dist\Lantai-Setup-1.2.3.exe scripts\pack\desktop.nsi

!include "MUI2.nsh"
!define MUI_ABORTWARNING
; Use custom icon from unpacked env (copied by build_win.ps1)
!define MUI_ICON "${UNPACKED}\icon.ico"
!define MUI_UNICON "${UNPACKED}\icon.ico"

!ifndef LANTAI_VERSION
  !error "Independently expected LANTAI_VERSION is required"
!endif
!ifndef LANTAI_NUMERIC_VERSION
  !error "LANTAI_NUMERIC_VERSION is required for PE version identity"
!endif
!ifndef LANTAI_SOURCE_SHA
  !error "Independently expected LANTAI_SOURCE_SHA is required"
!endif
!ifndef LANTAI_BUILD_IDENTITY_SHA256
  !error "Actual payload build identity digest is required"
!endif
!ifndef OUTPUT_EXE
  !define OUTPUT_EXE "dist\Lantai-Setup-${LANTAI_VERSION}.exe"
!endif

Name "Lantai Desktop"
OutFile "${OUTPUT_EXE}"
InstallDir "$LOCALAPPDATA\Lantai"
InstallDirRegKey HKCU "Software\Lantai" "InstallPath"
RequestExecutionLevel user
VIProductVersion "${LANTAI_NUMERIC_VERSION}"
VIAddVersionKey /LANG=2052 "ProductName" "Lantai Desktop"
VIAddVersionKey /LANG=2052 "ProductVersion" "${LANTAI_VERSION}"
VIAddVersionKey /LANG=2052 "FileVersion" "${LANTAI_VERSION}"
VIAddVersionKey /LANG=2052 "FileDescription" "Lantai Desktop installer"
VIAddVersionKey /LANG=2052 "Comments" "source=${LANTAI_SOURCE_SHA};identity=${LANTAI_BUILD_IDENTITY_SHA256}"

!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "SimpChinese"

; Pass /DUNPACKED=full_path from build_win.ps1 so path works when cwd != repo root
!ifndef UNPACKED
  !define UNPACKED "dist\win-unpacked"
!endif

Section "Lantai Desktop" SEC01
  SetOutPath "$INSTDIR"
  File /r "${UNPACKED}\*.*"
  WriteRegStr HKCU "Software\Lantai" "InstallPath" "$INSTDIR"
  WriteUninstaller "$INSTDIR\Uninstall.exe"

  ; Main shortcut - uses VBS to hide console window
  CreateShortcut "$SMPROGRAMS\Lantai Desktop.lnk" "$INSTDIR\Lantai Desktop.vbs" "" "$INSTDIR\icon.ico" 0
  CreateShortcut "$DESKTOP\Lantai Desktop.lnk" "$INSTDIR\Lantai Desktop.vbs" "" "$INSTDIR\icon.ico" 0
  
  ; Debug shortcut - shows console window for troubleshooting
  CreateShortcut "$SMPROGRAMS\Lantai Desktop (Debug).lnk" "$INSTDIR\Lantai Desktop (Debug).bat" "" "$INSTDIR\icon.ico" 0
SectionEnd

Section "Uninstall"
  Delete "$SMPROGRAMS\Lantai Desktop.lnk"
  Delete "$SMPROGRAMS\Lantai Desktop (Debug).lnk"
  Delete "$DESKTOP\Lantai Desktop.lnk"
  RMDir /r "$INSTDIR"
  DeleteRegKey HKCU "Software\Lantai"
SectionEnd
