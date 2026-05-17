Option Explicit

Dim shell, fso, appDir, bootstrap, appExe, rc, logPath, flagPath, visibleMode, cmd
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

appDir = fso.GetParentFolderName(WScript.ScriptFullName)
If Right(appDir, 1) <> "\" Then appDir = appDir & "\"

bootstrap = appDir & "bootstrap_runtime.bat"
appExe = appDir & "Ako-ai.exe"
logPath = shell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Ako-ai\runtime\bootstrap_runtime.log"
flagPath = shell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Ako-ai\runtime\bootstrap_ok.flag"

If Not fso.FileExists(bootstrap) Then
    MsgBox "bootstrap_runtime.bat was not found." & vbCrLf & bootstrap, vbCritical, "Ako"
    WScript.Quit 1
End If

If Not fso.FileExists(flagPath) Then
    shell.Popup "Ako first-run setup is starting. This can take several minutes.", 3, "Ako", 64
End If

visibleMode = 0
cmd = """" & bootstrap & """ --no-pause"

rc = shell.Run(cmd, visibleMode, True)
If rc <> 0 Then
    MsgBox "Ako runtime setup failed." & vbCrLf & _
           "Please check this log file:" & vbCrLf & logPath, _
           vbExclamation, "Ako"
    WScript.Quit rc
End If

If fso.FileExists(appExe) Then
    shell.Run """" & appExe & """", 1, False
Else
    MsgBox "Ako-ai.exe was not found." & vbCrLf & appExe, vbCritical, "Ako"
    WScript.Quit 1
End If
