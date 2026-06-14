' Launch AoE4 Replay Launcher with no console window.
' Double-click this directly to open the panel. If the setup is incomplete or
' broken, a clear message is shown instead of failing silently.
Option Explicit
Dim sh, fso, root, py, pyw, checkCmd, rc
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = root
' Tell the app where the repo is, so a copied (pip install .) package still finds
' data/, config and the restic repo here rather than under site-packages.
sh.Environment("Process").Item("AOE4REPLAY_ROOT") = root

' Prefer the project's virtualenv; fall back to a system Python.
py  = root & "\.venv\Scripts\python.exe"
pyw = root & "\.venv\Scripts\pythonw.exe"
If Not fso.FileExists(pyw) Then
    py  = "python"
    pyw = "pythonw"
End If

' Hidden, waited check that the app imports — catches the common breakages:
' env not created / package not installed / wrong venv / broken editable install.
checkCmd = """" & py & """ -c ""import aoe4replay.panel"""
On Error Resume Next
rc = sh.Run(checkCmd, 0, True)
If Err.Number <> 0 Then rc = 1   ' Python itself wasn't found -> treat as "not set up"
On Error GoTo 0
If rc <> 0 Then
    MsgBox "AoE4 Replay Launcher isn't set up yet (or its setup is broken)." & vbCrLf & vbCrLf & _
           "Double-click  setup.bat  in this folder to install it." & vbCrLf & _
           "(It needs Python 3.12+; setup.bat tells you if that's missing.)" & vbCrLf & vbCrLf & _
           "Then double-click this file again. See README.md for details.", _
           vbExclamation, "AoE4 Replay Launcher"
    WScript.Quit 1
End If

' Launch the GUI with no console window; any runtime error is reported by the app.
sh.Run """" & pyw & """ -m aoe4replay.cli panel", 1, False
