; LunarShroud NSIS uninstall hook.
;
; The app creates WebView2 profile data and diagnostic logs next to its exe
; (AGENTS 2.18: exe-directory data is required under restricted tokens).
; The installer owns the exe directory, so uninstall must remove those
; app-generated cache/log artifacts; otherwise the directory survives the
; uninstaller and leaves a large WebView2 profile behind.
;
; User data (config/presets/history) lives in %APPDATA%\LunarShroud (or the
; writable fallback) and is intentionally NOT touched here; deleting it is the
; uninstaller's "delete app data" decision.
; The Tauri NSIS template writes the uninstall entry through SHCTX. On this
; per-user build that key has been observed missing after a normal desktop
; install, so write the same per-user entry explicitly under HKCU as a
; belt-and-braces POSTINSTALL hook. The uninstaller's currentUser branch
; deletes HKCU\...\Uninstall\LunarShroud, so this stays symmetric.
!macro NSIS_HOOK_POSTINSTALL
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\LunarShroud" "DisplayName" "${PRODUCTNAME}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\LunarShroud" "DisplayIcon" "$INSTDIR\LunarShroud.exe"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\LunarShroud" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\LunarShroud" "Publisher" "${MANUFACTURER}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\LunarShroud" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\LunarShroud" "UninstallString" "$\"$INSTDIR\uninstall.exe$\""
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\LunarShroud" "NoModify" 1
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\LunarShroud" "NoRepair" 1
!macroend

!macro NSIS_HOOK_PREUNINSTALL
  RMDir /r "$INSTDIR\webview2-data"
  RMDir /r "$INSTDIR\runtime-temp"
  RMDir /r "$INSTDIR\logs"
  ; The app falls back to LocalAppData when the install directory is not
  ; writable. Those are cache/temp artifacts, never user config, so clean them
  ; without touching the LunarShroud data directory itself.
  RMDir /r "$LOCALAPPDATA\LunarShroud\webview2-data"
  RMDir /r "$LOCALAPPDATA\LunarShroud\runtime-temp"
  Delete "$INSTDIR\launcher-boot.log"
  Delete "$INSTDIR\debug.log"
  Delete "$INSTDIR\backend\backend-child.log"
  ; Remove the backend directory only if it is empty; a restricted-run
  ; fallback data dir ($INSTDIR\backend\data) is user data and must survive.
  RMDir "$INSTDIR\backend"
!macroend
