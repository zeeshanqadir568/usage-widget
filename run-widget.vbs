' Launches the usage widget with no console window.
' Double-click this file, or drop a shortcut to it in your Startup folder.
Option Explicit
Dim sh, fso, here, pyw, candidates, c
Set sh  = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)

' Prefer a real pythonw.exe; fall back to whatever is on PATH.
candidates = Array( _
    sh.ExpandEnvironmentStrings("%LOCALAPPDATA%\Python\pythoncore-3.14-64\pythonw.exe"), _
    sh.ExpandEnvironmentStrings("%LOCALAPPDATA%\Python\bin\pythonw.exe"), _
    "pythonw.exe")

pyw = "pythonw.exe"
For Each c In candidates
    If c = "pythonw.exe" Then
        pyw = c
        Exit For
    ElseIf fso.FileExists(c) Then
        pyw = c
        Exit For
    End If
Next

sh.CurrentDirectory = here
sh.Run """" & pyw & """ """ & here & "\usage_widget.pyw""", 0, False
