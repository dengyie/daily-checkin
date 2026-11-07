Option Explicit
Dim shell, rc
Set shell = CreateObject("WScript.Shell")
rc = shell.Run("powershell.exe -NoProfile -ExecutionPolicy Bypass -File ""E:\project\daily-checkin\scripts\launch-dispatch-agent-win.ps1""", 0, False)
WScript.Quit 0
