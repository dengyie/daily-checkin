Option Explicit
Dim shell
Set shell = CreateObject("WScript.Shell")
shell.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -File ""E:\project\daily-checkin\scripts\start-web-win.ps1""", 0, True
WScript.Quit 0
