Option Explicit

Dim fso, shell, root, launcherPath, appName, version, result, localPython
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
root = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
launcherPath = fso.BuildPath(root, "scripts\launch_windows.py")

If WScript.Arguments.Count <> 1 Then
    shell.Popup "This shortcut needs one app name: desktop or web.", 0, "Fluke 8845A", 48
    WScript.Quit 2
End If
appName = LCase(WScript.Arguments(0))
If appName <> "desktop" And appName <> "web" Then
    shell.Popup "Unknown app shortcut.", 0, "Fluke 8845A", 48
    WScript.Quit 2
End If

If Not fso.FileExists(launcherPath) Then
    shell.Popup "Cannot find the Fluke 8845A launcher in the extracted project folder.", 0, "Fluke 8845A", 48
    WScript.Quit 2
End If

localPython = fso.BuildPath(root, ".venv\Scripts\pythonw.exe")
If HasLocalPython(localPython) Then
    result = RunLocalPython(localPython)
Else
    version = SelectPython()
    If version = "" Then
        shell.Popup "Install 64-bit Python 3.11 or 3.12, then run this shortcut again.", 0, "Fluke 8845A", 48
        WScript.Quit 2
    End If
    If Not fso.FileExists(fso.BuildPath(root, ".venv\Scripts\python.exe")) Then
        shell.Popup "First launch installs the app dependencies. Please wait.", 8, "Fluke 8845A", 64
    End If
    result = RunHidden(version)
End If
If result <> 0 Then
    shell.Popup "Startup failed. Check the log:" & vbCrLf & shell.ExpandEnvironmentStrings("%LOCALAPPDATA%\Fluke8845A\logs\launcher.log"), 0, "Fluke 8845A", 48
End If
WScript.Quit result

Function SelectPython()
    If HasPython("3.12") Then
        SelectPython = "3.12"
    ElseIf HasPython("3.11") Then
        SelectPython = "3.11"
    Else
        SelectPython = ""
    End If
End Function

Function HasPython(pythonVersion)
    Dim command, exitCode
    command = "pyw.exe -" & pythonVersion & " -c " & Quote("import struct,sys; raise SystemExit(0 if sys.version_info[:2] == (" & Replace(pythonVersion, ".", ",") & ") and struct.calcsize('P') == 8 else 1)")
    On Error Resume Next
    exitCode = shell.Run(command, 0, True)
    HasPython = (Err.Number = 0 And exitCode = 0)
    Err.Clear
    On Error GoTo 0
End Function

Function HasLocalPython(pythonPath)
    Dim command, exitCode
    HasLocalPython = False
    If Not fso.FileExists(pythonPath) Then Exit Function
    command = Quote(pythonPath) & " -c " & Quote("import struct,sys; raise SystemExit(0 if sys.version_info[:2] in ((3,11),(3,12)) and struct.calcsize('P') == 8 else 1)")
    On Error Resume Next
    exitCode = shell.Run(command, 0, True)
    HasLocalPython = (Err.Number = 0 And exitCode = 0)
    Err.Clear
    On Error GoTo 0
End Function

Function RunHidden(pythonVersion)
    Dim command
    command = "pyw.exe -" & pythonVersion & " " & Quote(launcherPath) & " " & appName
    On Error Resume Next
    RunHidden = shell.Run(command, 0, True)
    If Err.Number <> 0 Then RunHidden = 1
    Err.Clear
    On Error GoTo 0
End Function

Function RunLocalPython(pythonPath)
    Dim command
    command = Quote(pythonPath) & " " & Quote(launcherPath) & " " & appName
    On Error Resume Next
    RunLocalPython = shell.Run(command, 0, True)
    If Err.Number <> 0 Then RunLocalPython = 1
    Err.Clear
    On Error GoTo 0
End Function

Function Quote(value)
    Quote = Chr(34) & value & Chr(34)
End Function
