# Keep the OpenSSH command short. The first line on stdin contains the repository's UTF-8 installer.
$ErrorActionPreference = 'Stop'
$line = [Console]::In.ReadLine()
if ([string]::IsNullOrWhiteSpace($line)) { throw 'Missing Antigravity installer.' }
$source = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($line))
& ([ScriptBlock]::Create($source))
