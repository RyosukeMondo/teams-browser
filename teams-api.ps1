# Convenience wrapper: .\teams-api.ps1   (identical to .\teams.ps1 serve)
& "$PSScriptRoot\.venv\Scripts\python.exe" -X utf8 -m teams_browser serve @args
