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
!macro NSIS_HOOK_PREUNINSTALL
  RMDir /r "$INSTDIR\webview2-data"
  RMDir /r "$INSTDIR\runtime-temp"
  RMDir /r "$INSTDIR\logs"
  Delete "$INSTDIR\launcher-boot.log"
  Delete "$INSTDIR\debug.log"
  Delete "$INSTDIR\backend\backend-child.log"
  ; Remove the backend directory only if it is empty; a restricted-run
  ; fallback data dir ($INSTDIR\backend\data) is user data and must survive.
  RMDir "$INSTDIR\backend"
!macroend
