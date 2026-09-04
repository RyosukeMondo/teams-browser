# Convenience wrapper: .\teams.ps1 chats   /   .\teams.ps1 read "Sam"
& "$PSScriptRoot\.venv\Scripts\python.exe" -X utf8 -m teams_browser @args
