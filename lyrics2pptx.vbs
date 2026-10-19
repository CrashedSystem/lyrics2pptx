' lyrics2pptx.vbs - launch the local web UI with no console window.
'
' Double-click this file (wscript.exe). It:
'   0. checks output\webui.state: if a server is already running (the same
'      instance id answers on /api/ping), it just opens that page -- a second
'      double-click must not start a second server (ADR-032),
'   1. otherwise finds pythonw.exe,
'   2. asks getport.py for a free loopback port,
'   3. starts "pythonw -m lyrics2pptx.webapp" hidden, logging to output\launch.log,
'   4. waits for the "http://127.0.0.1:<port>" line and opens the browser,
'   5. shows a dialog with the log path if it never comes up.
'
' There is no "stop" file to remember: the page tells the server when the tab
' closes and the server retires itself (webapp.py, ADR-032). Closing the browser
' window is the whole shutdown procedure.
'
' IMPORTANT (research report section 6): wscript reads a .vbs file as the
' system ANSI code page (cp949 on Korean Windows) and dies on a leading UTF-8
' BOM. Keep this file ASCII-only and save it as UTF-8 without BOM.

Option Explicit

Dim fso, shell, scriptDir, outDir, logFile
Dim pythonw, port, cmd, url, text, existing

Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
outDir = scriptDir & "\output"
logFile = outDir & "\launch.log"

If Not fso.FolderExists(outDir) Then
    fso.CreateFolder(outDir)
End If

pythonw = FindPythonW(fso, shell)
If pythonw = "" Then
    MsgBox "Python was not found. Install Python 3.12, then run the commands in README section 1.", 16, "lyrics2pptx"
    WScript.Quit 1
End If

' One server, one tab: reuse the running instance instead of starting another.
existing = TryReuse(fso, scriptDir)
If existing <> "" Then
    If shell.ExpandEnvironmentStrings("%LYRICS2PPTX_LAUNCH_DRYRUN%") <> "1" Then
        shell.Run existing, 1, False
    End If
    WScript.Quit 0
End If

port = FindFreePort(shell, pythonw, scriptDir)
If port = "" Then
    MsgBox "Could not choose a free port. Is Python working? See README section 1.", 16, "lyrics2pptx"
    WScript.Quit 1
End If

If fso.FileExists(logFile) Then
    fso.DeleteFile logFile, True
End If

cmd = "cmd /c set LYRICS2PPTX_LAUNCH_DRYRUN=" _
    & "&& set LYRICS2PPTX_BIND_PORT=" & port _
    & "&& set PYTHONPATH=" & scriptDir & "\src" _
    & "&& set PYTHONIOENCODING=utf-8" _
    & "&& " & Quote(pythonw) & " -m lyrics2pptx.webapp > " & Quote(logFile) & " 2>&1 < NUL"

shell.Run cmd, 0, False

url = WaitForUrl(fso, logFile, 40)
If url = "" Then
    text = ReadFile(fso, logFile)
    MsgBox "The server did not start in time." & vbCrLf & vbCrLf _
        & "Log: " & logFile & vbCrLf & vbCrLf & Left(text, 800), 16, "lyrics2pptx"
    WScript.Quit 1
End If

' LYRICS2PPTX_LAUNCH_DRYRUN=1 checks the launch chain without opening a browser.
If shell.ExpandEnvironmentStrings("%LYRICS2PPTX_LAUNCH_DRYRUN%") <> "1" Then
    shell.Run url, 1, False
End If
WScript.Quit 0


' --- reuse a running server (ADR-032) -------------------------------------

Function ReadStateValue(text, key)
    ' output\webui.state is plain "key=value" lines so VBScript can read it.
    Dim lines, index, line, pos, name
    ReadStateValue = ""
    lines = Split(text, vbLf)
    For index = 0 To UBound(lines)
        line = Trim(Replace(lines(index), vbCr, ""))
        If line <> "" And Left(line, 1) <> "#" Then
            pos = InStr(line, "=")
            If pos > 0 Then
                name = Trim(Left(line, pos - 1))
                If name = key Then
                    ReadStateValue = CleanToken(Mid(line, pos + 1))
                    Exit Function
                End If
            End If
        End If
    Next
End Function


Function ExtractJsonValue(text, key)
    ' Enough JSON for the flat /api/ping answer: "key": "value" or "key": 123.
    Dim marker, pos, rest, value, ch, index
    ExtractJsonValue = ""
    marker = """" & key & """"
    pos = InStr(text, marker)
    If pos = 0 Then Exit Function
    rest = Mid(text, pos + Len(marker))
    pos = InStr(rest, ":")
    If pos = 0 Then Exit Function
    rest = Trim(Mid(rest, pos + 1))
    If Left(rest, 1) = """" Then
        rest = Mid(rest, 2)
        index = InStr(rest, """")
        If index = 0 Then Exit Function
        ExtractJsonValue = Left(rest, index - 1)
    Else
        value = ""
        For index = 1 To Len(rest)
            ch = Mid(rest, index, 1)
            If ch = "," Or ch = "}" Or ch = " " Then Exit For
            value = value & ch
        Next
        ExtractJsonValue = value
    End If
End Function


Function HttpGet(url, timeoutMs)
    ' WinHttp when available, ServerXMLHTTP otherwise; both take a timeout so a
    ' hung socket can never freeze the double click.
    Dim http
    HttpGet = ""
    On Error Resume Next
    Set http = CreateObject("WinHttp.WinHttpRequest.5.1")
    If Err.Number <> 0 Then
        Err.Clear
        Set http = CreateObject("MSXML2.ServerXMLHTTP.6.0")
    End If
    If Err.Number <> 0 Then
        Err.Clear
        On Error Goto 0
        Exit Function
    End If
    http.SetTimeouts timeoutMs, timeoutMs, timeoutMs, timeoutMs
    http.Open "GET", url, False
    http.Send
    If Err.Number = 0 Then HttpGet = http.ResponseText
    Err.Clear
    On Error Goto 0
End Function


Function TryReuse(fso, scriptDir)
    ' Return the URL of the running server the state file describes, or "" when
    ' there is none (never started, killed, or another instance took it over).
    Dim stateFile, text, port, instance, body
    TryReuse = ""
    stateFile = scriptDir & "\output\webui.state"
    If Not fso.FileExists(stateFile) Then Exit Function
    text = ReadFile(fso, stateFile)
    If text = "" Then Exit Function
    port = ReadStateValue(text, "port")
    instance = ReadStateValue(text, "instance")
    If port = "" Or instance = "" Then Exit Function
    If Not IsNumeric(port) Then Exit Function
    body = HttpGet("http://127.0.0.1:" & port & "/api/ping", 1500)
    If body = "" Then Exit Function
    If ExtractJsonValue(body, "instance") = instance Then
        TryReuse = "http://127.0.0.1:" & port & "/"
    End If
End Function


Function Quote(value)
    Quote = Chr(34) & value & Chr(34)
End Function


Function CleanToken(value)
    ' VBScript Trim() removes spaces only, so strip CR/LF/tab explicitly.
    Dim result
    result = Replace(value, vbCrLf, " ")
    result = Replace(result, vbLf, " ")
    result = Replace(result, vbCr, " ")
    result = Replace(result, vbTab, " ")
    CleanToken = Trim(result)
End Function


Function ReadFile(fso, path)
    ' ReadAll raises "input past end of file" (error 62) on an empty file, and
    ' the launch log is empty for the moment between the redirect creating it
    ' and pythonw writing its first line -- so always guard before reading.
    Dim stream
    ReadFile = ""
    On Error Resume Next
    Set stream = fso.OpenTextFile(path, 1)
    If Err.Number <> 0 Then
        Err.Clear
        Exit Function
    End If
    If Not stream.AtEndOfStream Then
        ReadFile = stream.ReadAll()
    End If
    If Err.Number <> 0 Then
        Err.Clear
        ReadFile = ""
    End If
    stream.Close
    On Error Goto 0
End Function


Function LocateExecutable(shell, name)
    Dim exec, output
    On Error Resume Next
    Set exec = shell.Exec("cmd /c where " & name)
    If Err.Number <> 0 Then
        Err.Clear
        LocateExecutable = ""
        Exit Function
    End If
    On Error Goto 0
    output = ""
    If Not exec.StdOut.AtEndOfStream Then
        output = Trim(exec.StdOut.ReadAll())
    End If
    If InStr(output, vbCrLf) > 0 Then
        output = Split(output, vbCrLf)(0)
    End If
    LocateExecutable = CleanToken(output)
End Function


Function FindPythonW(fso, shell)
    Dim candidate, local
    candidate = LocateExecutable(shell, "pythonw.exe")
    If candidate <> "" Then
        FindPythonW = candidate
        Exit Function
    End If
    local = shell.ExpandEnvironmentStrings("%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe")
    If fso.FileExists(local) Then
        FindPythonW = local
        Exit Function
    End If
    local = shell.ExpandEnvironmentStrings("%LOCALAPPDATA%\Programs\Python\Python311\pythonw.exe")
    If fso.FileExists(local) Then
        FindPythonW = local
        Exit Function
    End If
    FindPythonW = ""
End Function


Function FindFreePort(shell, pythonw, scriptDir)
    Dim exec, output
    On Error Resume Next
    Set exec = shell.Exec(Quote(pythonw) & " " & Quote(scriptDir & "\getport.py"))
    If Err.Number <> 0 Then
        Err.Clear
        FindFreePort = ""
        Exit Function
    End If
    On Error Goto 0
    output = ""
    If Not exec.StdOut.AtEndOfStream Then
        output = exec.StdOut.ReadAll()
    End If
    FindFreePort = CleanToken(output)
End Function


Function FirstToken(value)
    Dim index, ch, result
    result = ""
    For index = 1 To Len(value)
        ch = Mid(value, index, 1)
        If ch = " " Or ch = ")" Or ch = vbCr Or ch = vbLf Or ch = vbTab Then
            Exit For
        End If
        result = result & ch
    Next
    FirstToken = result
End Function


Function WaitForUrl(fso, logFile, seconds)
    Dim attempt, text, pos, url
    For attempt = 1 To seconds * 2
        WScript.Sleep 500
        If fso.FileExists(logFile) Then
            text = ReadFile(fso, logFile)
            pos = InStr(text, "http://")
            If pos > 0 Then
                url = FirstToken(Mid(text, pos))
                If InStr(url, ":") > 0 Then
                    WaitForUrl = url
                    Exit Function
                End If
            End If
        End If
    Next
    WaitForUrl = ""
End Function
