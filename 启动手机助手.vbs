' Double-click launcher (no black console window)
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
d = fso.GetParentFolderName(WScript.ScriptFullName)
py = "C:\Users\Admin\AppData\Local\Programs\Python\Python312\pythonw.exe"
If Not fso.FileExists(py) Then py = "pythonw.exe"
sh.CurrentDirectory = d
sh.Run """" & py & """ """ & d & "\phone_mirror.py""", 0, False
