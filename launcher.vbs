Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
sh.Run "pythonw.exe """ & fso.GetParentFolderName(WScript.ScriptFullName) & "\launcher.py""", 0, False
