Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = Replace(WScript.ScriptFullName, WScript.ScriptName, "")
sh.Run "pythonw peeky.py", 0, False
