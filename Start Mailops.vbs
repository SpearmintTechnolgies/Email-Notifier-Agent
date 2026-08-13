' Double-click launcher: opens Mailops console and starts the agent.
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
bat = dir & "\Start Mailops.bat"
' WindowStyle 1 = normal visible console so you can see "Mailops started"
shell.Run """" & bat & """", 1, False
