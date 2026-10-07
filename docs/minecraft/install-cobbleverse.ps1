# Instala o COBBLEVERSE (cliente) na versão EXATA do servidor AsunBoid, direto do Modrinth.
# Cada arquivo é conferido pelo hash SHA-512 do modpack oficial: o resultado é idêntico ao do servidor.
# Uso: clique com o botão direito no arquivo > "Executar com o PowerShell"
#   ou: powershell -ExecutionPolicy Bypass -File .\install-cobbleverse.ps1
$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$VersionId = "4SKGla61"

$Default = Join-Path $env:APPDATA ".minecraft-cobbleverse"
$GameDir = Read-Host "Pasta do jogo (Enter = $Default)"
if ([string]::IsNullOrWhiteSpace($GameDir)) { $GameDir = $Default }
New-Item -ItemType Directory -Force -Path $GameDir | Out-Null
$GameFull = (Resolve-Path $GameDir).Path
$ModsDir = Join-Path $GameFull "mods"
$Marker = Join-Path $GameFull ".cobbleverse-version"

# Mods de outra versão (ou de outro modpack) vão para um backup, para não misturar
$Prev = if (Test-Path $Marker) { (Get-Content $Marker -Raw).Trim() } else { $null }
if ((Test-Path $ModsDir) -and ($Prev -ne $VersionId) -and (Get-ChildItem $ModsDir -File -ErrorAction SilentlyContinue)) {
    $Backup = "$ModsDir.backup-$(Get-Date -Format yyyyMMdd-HHmmss)"
    Move-Item $ModsDir $Backup
    Write-Host "Mods antigos movidos para: $Backup" -ForegroundColor Yellow
}
Set-Content -Path $Marker -Value $VersionId

# Modpack oficial (.mrpack) da versão do servidor
$Version = Invoke-RestMethod "https://api.modrinth.com/v2/version/$VersionId"
$Primary = $Version.files | Where-Object { $_.primary } | Select-Object -First 1
Write-Host "COBBLEVERSE $($Version.version_number)" -ForegroundColor Cyan
$Tmp = Join-Path $env:TEMP "cobbleverse-$VersionId"
Remove-Item $Tmp -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $Tmp | Out-Null
$Zip = Join-Path $Tmp "pack.zip"
Invoke-WebRequest $Primary.url -OutFile $Zip -UseBasicParsing
Expand-Archive $Zip -DestinationPath (Join-Path $Tmp "pack") -Force
$Index = Get-Content (Join-Path $Tmp "pack\modrinth.index.json") -Raw -Encoding UTF8 | ConvertFrom-Json

# Baixa cada arquivo do cliente e confere o SHA-512 (pula os que já estão corretos)
$Files = @($Index.files | Where-Object { -not $_.env -or $_.env.client -ne "unsupported" })
$i = 0; $Fail = 0
foreach ($f in $Files) {
    $i++
    $Dest = [IO.Path]::GetFullPath((Join-Path $GameFull $f.path))
    if (-not $Dest.StartsWith($GameFull)) { throw "Caminho inválido no modpack: $($f.path)" }
    New-Item -ItemType Directory -Force -Path (Split-Path $Dest) | Out-Null
    $Expected = $f.hashes.sha512.ToLower()
    if ((Test-Path $Dest) -and ((Get-FileHash $Dest -Algorithm SHA512).Hash.ToLower() -eq $Expected)) { continue }
    Write-Progress -Activity "Baixando o modpack" -Status "$i/$($Files.Count) $($f.path)" -PercentComplete ($i * 100 / $Files.Count)
    $Ok = $false
    foreach ($Url in $f.downloads) {
        try {
            Invoke-WebRequest $Url -OutFile $Dest -UseBasicParsing
            if ((Get-FileHash $Dest -Algorithm SHA512).Hash.ToLower() -eq $Expected) { $Ok = $true; break }
        } catch { }
    }
    if (-not $Ok) { Write-Host "FALHOU: $($f.path)" -ForegroundColor Red; $Fail++ }
}

# Configurações do modpack
foreach ($o in "overrides", "client-overrides") {
    $Src = Join-Path $Tmp "pack\$o"
    if (Test-Path $Src) { Copy-Item (Join-Path $Src "*") $GameFull -Recurse -Force }
}

Write-Host ""
Write-Host "Minecraft: $($Index.dependencies.minecraft)  |  Fabric Loader: $($Index.dependencies.'fabric-loader')" -ForegroundColor Cyan
Write-Host "Pasta do jogo: $GameFull"
if ($Fail -gt 0) {
    Write-Host "$Fail arquivo(s) falharam. Rode o script de novo: ele continua de onde parou." -ForegroundColor Red
    Read-Host "Enter para sair"; exit 1
}
Write-Host "Pronto! $($Files.Count) arquivos conferidos pelo hash." -ForegroundColor Green
Read-Host "Enter para sair"
