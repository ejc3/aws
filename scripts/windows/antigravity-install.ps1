# Run in the intended user's profile. Stdin is the base64-encoded UTF-8 fleet model guide.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
if ([Security.Principal.WindowsIdentity]::GetCurrent().IsSystem) { throw 'Run as the Windows user.' }
if (($env:PROCESSOR_ARCHITEW6432, $env:PROCESSOR_ARCHITECTURE) -notcontains 'AMD64') { throw 'Colin PC needs the Windows x64 build.' }
$guide = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String([Console]::In.ReadToEnd()))
if ([string]::IsNullOrWhiteSpace($guide)) { throw 'Empty model guide.' }
$bin = Join-Path $env:LOCALAPPDATA 'agy\bin'
$exe = Join-Path $bin 'agy.exe'
# File.Replace preserves the previous complete file if a running process denies the update.
function Publish-File($source, $target) {
  if ([IO.File]::Exists($target)) {
    $backup = "$source.bak"
    [IO.File]::Replace($source, $target, $backup)
    [IO.File]::Delete($backup)
  } else { [IO.File]::Move($source, $target) }
}
function Get-AgyVersion($path) {
  if (Test-Path -LiteralPath $path -PathType Leaf) {
    try {
      $v = & $path --version 2>$null
      if ($LASTEXITCODE -eq 0 -and $v -is [string] -and $v -match '^\d+\.\d+\.\d+([+-][\w.+-]+)?$') { return "$v" }
    } catch { Write-Verbose 'Existing executable could not run.' }
  }
}
# Google publishes this executable and SHA-512 in its windows_amd64 CLI manifest. agy self-updates.
$version = Get-AgyVersion $exe
if (!$version) {
  [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
  [IO.Directory]::CreateDirectory($bin) | Out-Null
  $tmp = Join-Path $bin ('.agy-' + [Guid]::NewGuid() + '.exe')
  try {
    $url = 'https://storage.googleapis.com/antigravity-public/antigravity-cli/1.5.18-6386224290725888/windows-x64/cli_windows_x64.exe'
    $sha = '53605db7013dfed51e8c416d51e0c17a3b5ad0750c13da5669954ba4a281318746be0cf8cde2b256cfdb4bfbdade4badad1a0503931f8458416184549afa06b6'
    Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $tmp
    $hash = [Security.Cryptography.SHA512]::Create()
    $inputStream = [IO.File]::OpenRead($tmp)
    try {
      $actual = [BitConverter]::ToString($hash.ComputeHash($inputStream)).Replace('-', '')
    } finally { $inputStream.Dispose(); $hash.Dispose() }
    if ($actual -ne $sha) { throw 'Antigravity SHA-512 mismatch.' }
    Unblock-File $tmp
    $version = Get-AgyVersion $tmp
    if ($version -ne '1.5.18') { throw 'Unexpected Antigravity version.' }
    Publish-File $tmp $exe
  } finally { if (Test-Path $tmp) { Remove-Item -LiteralPath $tmp } }
}
# Keep literal %VARIABLE% entries and the registry value kind intact across SSH logins.
$envKey = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey('Environment')
try {
  $userPath = [string]$envKey.GetValue('Path', '', [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
  $dirs = @($userPath -split ';' | ForEach-Object { [Environment]::ExpandEnvironmentVariables($_).TrimEnd('\') })
  if ($dirs -notcontains $bin) {
    $kind = if ($envKey.GetValueNames() -contains 'Path') { $envKey.GetValueKind('Path') } else { 'ExpandString' }
    $newPath = if ($userPath) { "$bin;$userPath" } else { $bin }
    $envKey.SetValue('Path', $newPath, [Microsoft.Win32.RegistryValueKind]$kind)
    # Notify desktop applications in this Windows session that future children can use the new PATH.
    Add-Type -Namespace Ejc3 -Name EnvironmentChange -MemberDefinition @'
[DllImport("user32.dll", CharSet = CharSet.Unicode)]
public static extern IntPtr SendMessageTimeout(IntPtr hwnd, uint message, UIntPtr wParam,
    string lParam, uint flags, uint timeout, out UIntPtr result);
'@
    $result = [UIntPtr]::Zero
    [void][Ejc3.EnvironmentChange]::SendMessageTimeout([IntPtr]0xffff, 0x1a, [UIntPtr]::Zero, 'Environment', 2, 1000, [ref]$result)
  }
} finally { $envKey.Dispose() }
# Keep UTF-8 BOMs, personal text and line endings intact in existing user instruction files.
$utf8 = New-Object System.Text.UTF8Encoding($false, $true)
function Read-Rule($path) {
  if ([IO.File]::Exists($path)) { return $utf8.GetString([IO.File]::ReadAllBytes($path)) }
  return ''
}
function Write-Rule($path, $old, $content, $requireNew = $false) {
  if ($old -ceq $content) { return }
  $dir = [IO.Path]::GetDirectoryName($path)
  [IO.Directory]::CreateDirectory($dir) | Out-Null
  if ((Test-Path -LiteralPath $path) -and ((Get-Item -Force -LiteralPath $path).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
    throw "Instruction file is linked; preserving $path."
  }
  $tmp = Join-Path $dir ('.agy-' + [Guid]::NewGuid() + '.tmp')
  try {
    [IO.File]::WriteAllBytes($tmp, $utf8.GetBytes($content))
    if ((Read-Rule $path) -cne $old) { throw "Concurrent edit to $path; retry later." }
    if ($requireNew) { [IO.File]::Move($tmp, $path) } else { Publish-File $tmp $path }
  } finally { if ([IO.File]::Exists($tmp)) { [IO.File]::Delete($tmp) } }
}
$body = $guide.TrimEnd("`r","`n") + "`n"
$native = Join-Path $env:USERPROFILE '.gemini\config\rules\ejc3-antigravity-models.md'
Write-Rule $native (Read-Rule $native) ("---`ntrigger: always_on`n---`n" + $body)

# Use the same bounded section as the macOS install. Keep all personal bytes on either side.
function Set-ModelSection($path) {
  $text = Read-Rule $path
  $begin = '<!-- BEGIN ejc3-antigravity-models (managed) -->'
  $end = '<!-- END ejc3-antigravity-models (managed) -->'
  $block = "$begin`n$body$end"
  if (!$text.Contains($begin) -and !$text.Contains($end)) {
    $separator = if ($text.Length) { "`n`n" } else { '' }
    $new = $text + $separator + $block + "`n"
  } else {
    $startCount = [regex]::Matches($text, [regex]::Escape($begin)).Count
    $endCount = [regex]::Matches($text, [regex]::Escape($end)).Count
    $pattern = '(?ms)^(\uFEFF?)' + [regex]::Escape($begin) + '\r?\n.*?^' + [regex]::Escape($end) + '(?=\r?$)'
    $match = [regex]::Match($text, $pattern)
    if ($startCount -ne 1 -or $endCount -ne 1 -or !$match.Success) { throw "Ambiguous managed section in $path; preserving personal instructions." }
    $new = $text.Remove($match.Index, $match.Length).Insert($match.Index, $match.Groups[1].Value + $block)
  }
  Write-Rule $path $text $new
}
$claude = if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { Join-Path $env:USERPROFILE '.claude' }
Set-ModelSection (Join-Path $claude 'rules\ejc3-antigravity-models.md')

# Codex reads one nonempty global instructions file, with AGENTS.override.md taking precedence.
$codex = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }
$path = Join-Path $codex 'AGENTS.override.md'
$text = Read-Rule $path
if ([string]::IsNullOrWhiteSpace($text.TrimStart([char]0xfeff))) {
  $path = Join-Path $codex 'AGENTS.md'
}
Set-ModelSection $path

# The CLI can remove the permission key when a user selects its review default.
# Mark the first successful seed so an hourly run never restores an unwanted value.
function Set-InitialToolPermission {
  $dir = Join-Path $env:USERPROFILE '.gemini\antigravity-cli'
  [IO.Directory]::CreateDirectory($dir) | Out-Null
  $marker = Join-Path $dir '.fleet-permission-default-v1'
  if ((Test-Path -LiteralPath $marker) -and ((Get-Item -Force -LiteralPath $marker).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
    throw "Preference marker is linked; preserving $marker."
  }
  $seed = [IO.File]::Open($marker, 'OpenOrCreate', 'ReadWrite', 'None')
  try {
    $done = [Text.Encoding]::ASCII.GetBytes("done`n")
    $saved = New-Object byte[] $done.Length
    if ($seed.Length -eq $done.Length -and $seed.Read($saved, 0, $saved.Length) -eq $saved.Length -and
        [Text.Encoding]::ASCII.GetString($saved) -ceq "done`n") { return }

    $path = Join-Path $dir 'settings.json'
    $exists = [IO.File]::Exists($path)
    $old = Read-Rule $path
    $source = if ($exists) { $old } else { '{}' }
    # Read top-level names only. Insert into the original UTF-8 text, retaining
    # personal rules, number precision, escaping, formatting and any BOM.
    try {
      Add-Type -AssemblyName System.Runtime.Serialization
      $bytes = $utf8.GetBytes($source.TrimStart([char]0xfeff))
      $reader = [Runtime.Serialization.Json.JsonReaderWriterFactory]::CreateJsonReader($bytes, [Xml.XmlDictionaryReaderQuotas]::Max)
      $document = New-Object Xml.XmlDocument
      try { $document.Load($reader) } finally { $reader.Dispose() }
      $object = $document.DocumentElement
      if ($object.GetAttribute('type') -ne 'object') { throw 'Not an object.' }
    } catch { throw 'Expected a valid JSON object in the Antigravity CLI settings; preserving the file.' }
    $members = @($object.SelectNodes('*') | ForEach-Object { $_.get_LocalName() })
    if ($members -notcontains 'toolPermission') {
      $entry = '"toolPermission":"always-proceed"'
      if ($members.Count -gt 0) { $entry += ',' }
      Write-Rule $path $old ($source.Insert($source.IndexOf('{') + 1, $entry)) (!$exists)
    }
    $seed.Position = 0
    $seed.SetLength(0)
    $seed.Write($done, 0, $done.Length)
    $seed.Flush($true)
  } finally { $seed.Dispose() }
}
Set-InitialToolPermission
"Antigravity $version and model guides installed for $env:USERNAME"
