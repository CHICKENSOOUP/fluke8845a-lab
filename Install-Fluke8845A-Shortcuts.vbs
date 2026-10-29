Option Explicit

Dim fso, shell, root, iconPath, desktopPath, windowlessHost
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
root = fso.GetParentFolderName(WScript.ScriptFullName)
iconPath = fso.BuildPath(root, "assets\fluke8845a_app_icon_v3.ico")
desktopPath = shell.SpecialFolders("Desktop")
windowlessHost = fso.BuildPath(fso.GetParentFolderName(WScript.FullName), "wscript.exe")

CreateAppShortcut "Fluke 8845A", "desktop"
CreateAppShortcut "Fluke 8845A Web", "web"

shell.Run Quote(windowlessHost) & " " & Quote(fso.BuildPath(root, "scripts\launch_app.vbs")) & " desktop", 0, False

Sub CreateAppShortcut(shortcutName, appName)
    Dim shortcut
    Set shortcut = shell.CreateShortcut(fso.BuildPath(desktopPath, shortcutName & ".lnk"))
    shortcut.TargetPath = windowlessHost
    shortcut.Arguments = Quote(fso.BuildPath(root, "scripts\launch_app.vbs")) & " " & appName
    shortcut.WorkingDirectory = root
    shortcut.IconLocation = iconPath & ",0"
    shortcut.Description = shortcutName
    shortcut.Save
End Sub

Function Quote(value)
    Quote = Chr(34) & value & Chr(34)
End Function
