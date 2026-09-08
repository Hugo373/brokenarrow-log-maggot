Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
base = files.GetParentFolderName(WScript.ScriptFullName)
urlFile = base & "\ba-webui.url"
runtimePy = base & "\runtime\python.exe"

pythonCmd = "python"
If files.FileExists(runtimePy) Then pythonCmd = """" & runtimePy & """"

Function ReadUrl()
    ReadUrl = ""
    If files.FileExists(urlFile) Then
        On Error Resume Next
        Set f = files.OpenTextFile(urlFile, 1)
        If Not f.AtEndOfStream Then ReadUrl = Trim(f.ReadLine)
        f.Close
        On Error GoTo 0
    End If
End Function

Function Alive(u)
    Alive = False
    If Len(u) = 0 Then Exit Function
    On Error Resume Next
    Set http = CreateObject("WinHttp.WinHttpRequest.5.1")
    http.SetTimeouts 1200, 1200, 1200, 1200
    http.Open "GET", Split(u & "|", "|")(0) & "/api/state", False
    http.Send
    If Err.Number = 0 Then
        If http.Status = 200 Then Alive = True
    End If
    On Error GoTo 0
End Function

u = ReadUrl()
oldContent = u
If Not Alive(u) Then
    shell.Run "cmd /c cd /d """ & base & """ && " & pythonCmd & " web_ui.py --no-browser", 0, False
    ok = False
    For i = 1 To 40
        WScript.Sleep 300
        c = ReadUrl()
        If Len(c) > 0 And c <> oldContent Then
            u = c
            If Alive(u) Then
                ok = True
                Exit For
            End If
        End If
    Next
    If Not ok Then
        MsgBox "BA工具启动失败：12 秒内未等到本地服务。" & vbCrLf & "请确认 python 在 PATH 中，或手动运行: python web_ui.py", 48, "Broken Arrow Log Tool"
        WScript.Quit
    End If
End If
shell.Run Split(u & "|", "|")(0), 1, False
