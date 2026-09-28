' Launch the NVR archiver in the background without a console window.
' This file is intentionally ASCII-only so that it works regardless of the
' encoding settings of the folder it lives in.
Option Explicit

Dim fso, sh, baseDir, script, cmd
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")

baseDir = fso.GetParentFolderName(WScript.ScriptFullName)
script = fso.BuildPath(baseDir, "nvr_puller.py")

sh.CurrentDirectory = baseDir

cmd = "pythonw.exe """ & script & """"
sh.Run cmd, 0, False
