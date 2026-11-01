Option Explicit
Dim shell
Set shell = CreateObject("WScript.Shell")
shell.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -File ""E:\project\daily-checkin\scripts\run-checkin-win.ps1""", 0, True
WScript.Quit 0
