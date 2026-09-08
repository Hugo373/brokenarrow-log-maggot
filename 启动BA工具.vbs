Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
base = files.GetParentFolderName(WScript.ScriptFullName)
command = "cmd /c cd /d """ & base & """ && python web_ui.py"
shell.Run command, 0, False
