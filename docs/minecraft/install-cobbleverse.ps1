<#
  Instalador do cliente COBBLEVERSE (servidor AsunBoid)

  O que faz:
    1. Baixa do Modrinth o modpack COBBLEVERSE na MESMA versao do servidor.
    2. Baixa cada mod/resource pack direto do CDN oficial e confere o SHA-512.
    3. Copia configs do modpack (overrides) para a pasta do jogo.
    4. Tira da pasta mods qualquer .jar que nao pertence ao modpack (vai para mods-antigos).

  Pode rodar de novo quantas vezes quiser: so baixa o que falta ou esta corrompido.

  Uso:
    Botao direito no arquivo > "Executar com o PowerShell"
    ou:  powershell -ExecutionPolicy Bypass -File .\install-cobbleverse.ps1

  Parametros opcionais:
    -PackVersion 1.7.3        numero ou ID da versao no Modrinth
    -GameDir D:\Jogos\cobble  pasta do jogo
    -NoPause                  nao espera Enter no final
#>
[CmdletBinding()]
param(
    [string]$PackVersion = '',
    [string]$GameDir = '',
    [string]$ApiBase = 'https://api.modrinth.com/v2',
    [switch]$NoPause
)

# ---------------------------------------------------------------------------
# Versao que o servidor roda. Preencha com o valor de MODRINTH_VERSION do
# statefulset (ID ou numero da versao) antes de mandar o script para o pessoal.
# Vazio = o script pergunta e sugere a ultima release para Minecraft 1.21.1.
$PinnedVersion = '4SKGla61'
# ---------------------------------------------------------------------------

$Project   = 'cobbleverse'
$McVersion = '1.21.1'
$Loader    = 'fabric'
$UserAgent = 'asunboid-cobbleverse-installer/1.0'
$KeepIfExists = @('options.txt', 'servers.dat')   # nao sobrescreve ajustes do jogador

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
} catch { }
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

function Write-Step([string]$Text) { Write-Host ''; Write-Host "== $Text" -ForegroundColor Cyan }

function Stop-Script([int]$Code) {
    if (-not $NoPause) { Write-Host ''; [void](Read-Host 'Enter para sair') }
    exit $Code
}

function Get-Json([string]$Url) {
    $resp = Invoke-WebRequest -Uri $Url -UseBasicParsing -UserAgent $UserAgent -TimeoutSec 60
    $text = $resp.Content
    if ($text -is [byte[]]) { $text = [Text.Encoding]::UTF8.GetString($text) }
    return ($text | ConvertFrom-Json)
}

function Get-Sha512([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA512).Hash.ToLowerInvariant()
}

# Baixa para um temporario de nome simples e so move para o destino se o hash bater.
function Save-File([string[]]$Urls, [string]$Dest, [string]$Sha512, [string]$Tmp) {
    if ([IO.File]::Exists($Dest) -and $Sha512 -and ((Get-Sha512 $Dest) -eq $Sha512)) { return 'ok' }
    [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($Dest))
    foreach ($url in $Urls) {
        for ($try = 1; $try -le 3; $try++) {
            try {
                if ([IO.File]::Exists($Tmp)) { [IO.File]::Delete($Tmp) }
                Invoke-WebRequest -Uri $url -OutFile $Tmp -UseBasicParsing -UserAgent $UserAgent -TimeoutSec 3600
                if ($Sha512 -and ((Get-Sha512 $Tmp) -ne $Sha512)) { throw 'SHA-512 diferente do esperado' }
                [IO.File]::Copy($Tmp, $Dest, $true)
                [IO.File]::Delete($Tmp)
                return 'baixado'
            } catch {
                Start-Sleep -Seconds (2 * $try)
            }
        }
    }
    if ([IO.File]::Exists($Tmp)) { [IO.File]::Delete($Tmp) }
    return 'falhou'
}

try {
    Write-Host 'Instalador COBBLEVERSE - AsunBoid' -ForegroundColor Green

    # ---------------- versao do modpack ----------------
    Write-Step 'Consultando versoes no Modrinth'
    $listUrl = "$ApiBase/project/$Project/version?game_versions=%5B%22$McVersion%22%5D&loaders=%5B%22$Loader%22%5D"
    $raw = Get-Json $listUrl
    $versions = @($raw | ForEach-Object { $_ })
    if ($versions.Count -eq 0) { throw "Nenhuma versao do $Project para Minecraft $McVersion ($Loader)." }

    $wanted = $PackVersion
    if (-not $wanted) { $wanted = $PinnedVersion }
    if (-not $wanted) {
        $latest = @($versions | Where-Object { $_.version_type -eq 'release' })
        if ($latest.Count -eq 0) { $latest = $versions }
        $default = $latest[0].version_number
        $wanted = $default
        if (-not $NoPause) {
            Write-Host 'Use a MESMA versao do servidor (pergunte no Discord se nao souber).'
            $answer = Read-Host "Versao do COBBLEVERSE [$default]"
            if ($answer) { $wanted = $answer.Trim() }
        }
    }

    $ver = @($versions | Where-Object { $_.id -eq $wanted -or $_.version_number -eq $wanted })
    if ($ver.Count -gt 0) {
        $ver = $ver[0]
    } else {
        try { $ver = Get-Json "$ApiBase/version/$wanted" } catch { $ver = $null }
        if (-not $ver -or $ver.project_id -eq $null) {
            $known = ($versions | Select-Object -First 8 | ForEach-Object { $_.version_number }) -join ', '
            throw "Versao '$wanted' nao encontrada. Disponiveis: $known"
        }
    }
    Write-Host ("Modpack: {0}  (versao {1}, id {2})" -f $ver.name, $ver.version_number, $ver.id)

    $packFile = @($ver.files | Where-Object { $_.primary })
    if ($packFile.Count -eq 0) { $packFile = @($ver.files | Where-Object { $_.filename -like '*.mrpack' }) }
    if ($packFile.Count -eq 0) { throw 'Esta versao nao tem arquivo .mrpack.' }
    $packFile = $packFile[0]

    # ---------------- pasta do jogo ----------------
    if (-not $GameDir) {
        if ($env:APPDATA) { $defaultDir = Join-Path $env:APPDATA '.minecraft-cobbleverse' }
        else { $defaultDir = Join-Path $HOME '.minecraft-cobbleverse' }
        if ($NoPause) { $GameDir = $defaultDir }
        else {
            Write-Host ''
            Write-Host 'Pasta do jogo: use uma pasta separada, nao a .minecraft que voce ja usa.'
            $answer = Read-Host "Pasta [$defaultDir]"
            if ($answer) { $GameDir = $answer.Trim().Trim('"') } else { $GameDir = $defaultDir }
        }
    }
    if (-not [IO.Path]::IsPathRooted($GameDir)) { $GameDir = Join-Path (Get-Location).Path $GameDir }
    $GameDir = [IO.Path]::GetFullPath($GameDir)
    [void][IO.Directory]::CreateDirectory($GameDir)
    $sep = [IO.Path]::DirectorySeparatorChar
    $gamePrefix = $GameDir.TrimEnd($sep) + $sep
    Write-Host "Pasta do jogo: $GameDir"

    function Resolve-InGameDir([string]$Rel) {
        $full = [IO.Path]::GetFullPath([IO.Path]::Combine($GameDir, $Rel))
        if (-not $full.StartsWith($gamePrefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Caminho fora da pasta do jogo: $Rel"
        }
        return $full
    }

    # O .mrpack fica em cache: rodar de novo nao baixa o modpack inteiro outra vez.
    $work = Join-Path $GameDir '.cobbleverse-cache'
    [void][IO.Directory]::CreateDirectory($work)
    $tmpFile = Join-Path $work 'download.part'

    # ---------------- baixa o .mrpack ----------------
    Write-Step 'Baixando o modpack'
    $mrpack = Join-Path $work ("pack-{0}.mrpack" -f $ver.id)
    foreach ($old in [IO.Directory]::GetFiles($work, 'pack-*.mrpack')) {
        if ($old -ne $mrpack) { [IO.File]::Delete($old) }
    }
    $packMb = [math]::Round($packFile.size / 1MB)
    Write-Host "$($packFile.filename) (~$packMb MB). Pode demorar alguns minutos, nao feche a janela."
    $packHash = ''
    if ($packFile.hashes -and $packFile.hashes.sha512) { $packHash = $packFile.hashes.sha512.ToLowerInvariant() }
    $state = Save-File @($packFile.url) $mrpack $packHash $tmpFile
    if ($state -eq 'falhou') { throw "Nao consegui baixar $($packFile.url)" }
    Write-Host "$($packFile.filename): $state"

    $expected = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    $failed = New-Object System.Collections.Generic.List[string]
    $countOk = 0; $countNew = 0; $countSkip = 0

    $zip = [IO.Compression.ZipFile]::OpenRead($mrpack)
    try {
        $entry = $zip.GetEntry('modrinth.index.json')
        if (-not $entry) { throw 'modrinth.index.json nao existe dentro do .mrpack.' }
        $reader = New-Object IO.StreamReader($entry.Open(), [Text.Encoding]::UTF8)
        try { $index = $reader.ReadToEnd() | ConvertFrom-Json } finally { $reader.Dispose() }

        $mcDep = $index.dependencies.minecraft
        $fabricDep = $index.dependencies.'fabric-loader'

        # ---------------- mods e demais arquivos ----------------
        $files = @($index.files | ForEach-Object { $_ })
        $clientFiles = @($files | Where-Object { -not ($_.env -and $_.env.client -eq 'unsupported') })
        $countSkip = $files.Count - $clientFiles.Count
        $totalMb = [math]::Round((($clientFiles | Measure-Object -Property fileSize -Sum).Sum) / 1MB)
        Write-Step ("Baixando {0} arquivos (~{1} MB, so o que faltar)" -f $clientFiles.Count, $totalMb)

        $n = 0
        foreach ($f in $clientFiles) {
            $n++
            $dest = Resolve-InGameDir $f.path
            [void]$expected.Add($dest)
            $hash = ''
            if ($f.hashes -and $f.hashes.sha512) { $hash = $f.hashes.sha512.ToLowerInvariant() }
            $state = Save-File @($f.downloads) $dest $hash $tmpFile
            switch ($state) {
                'ok'      { $countOk++ }
                'baixado' { $countNew++ }
                default   { $failed.Add($f.path) }
            }
            $color = 'Gray'
            if ($state -eq 'baixado') { $color = 'White' }
            if ($state -eq 'falhou')  { $color = 'Red' }
            Write-Host ("[{0}/{1}] {2,-8} {3}" -f $n, $clientFiles.Count, $state, $f.path) -ForegroundColor $color
        }

        # ---------------- overrides (configs do modpack) ----------------
        Write-Step 'Copiando configs do modpack'
        $countOverrides = 0
        foreach ($prefix in @('overrides/', 'client-overrides/')) {
            foreach ($e in $zip.Entries) {
                $name = $e.FullName.Replace('\', '/')
                if (-not $name.StartsWith($prefix) -or $name.EndsWith('/')) { continue }
                $rel = $name.Substring($prefix.Length)
                if (-not $rel) { continue }
                $dest = Resolve-InGameDir $rel
                [void]$expected.Add($dest)
                if (($KeepIfExists -contains $rel) -and [IO.File]::Exists($dest)) { continue }
                [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($dest))
                [IO.Compression.ZipFileExtensions]::ExtractToFile($e, $dest, $true)
                $countOverrides++
            }
        }
        Write-Host "$countOverrides arquivos copiados."
    } finally {
        $zip.Dispose()
    }

    # ---------------- tira mods que nao sao do modpack ----------------
    $modsDir = Join-Path $GameDir 'mods'
    $moved = 0
    if ([IO.Directory]::Exists($modsDir)) {
        $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
        $oldDir = Join-Path (Join-Path $GameDir 'mods-antigos') $stamp
        foreach ($jar in [IO.Directory]::GetFiles($modsDir, '*.jar')) {
            if ($expected.Contains($jar)) { continue }
            [void][IO.Directory]::CreateDirectory($oldDir)
            [IO.File]::Move($jar, (Join-Path $oldDir ([IO.Path]::GetFileName($jar))))
            $moved++
        }
        if ($moved -gt 0) {
            Write-Step 'Mods fora do modpack'
            Write-Host "$moved .jar movidos para $oldDir (causariam divergencia com o servidor)."
        }
    }

    [IO.File]::WriteAllText((Join-Path $GameDir '.cobbleverse-version'), "$($ver.version_number) $($ver.id)`r`n")

    # ---------------- resumo ----------------
    Write-Step 'Resumo'
    Write-Host ("Baixados agora: {0} | Ja estavam certos: {1} | So servidor (ignorados): {2} | Falhas: {3}" -f $countNew, $countOk, $countSkip, $failed.Count)
    if ($failed.Count -gt 0) {
        Write-Host 'Arquivos que falharam:' -ForegroundColor Red
        $failed | ForEach-Object { Write-Host "  $_" -ForegroundColor Red }
        Write-Host 'Rode o script de novo. Ele continua de onde parou.' -ForegroundColor Yellow
        Stop-Script 1
    }

    Write-Host ''
    Write-Host 'Pronto!' -ForegroundColor Green
    Write-Host 'Agora, no seu launcher:'
    Write-Host "  1. Versao: Fabric $fabricDep para Minecraft $mcDep"
    Write-Host "  2. Pasta do jogo (game directory): $GameDir"
    Write-Host '  3. Memoria: 6 GB (minimo 4 GB)'
    Write-Host '  4. Entre no servidor e digite /register <senha> <senha> na primeira vez'
    Stop-Script 0
}
catch {
    Write-Host ''
    Write-Host "ERRO: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host 'Nada foi removido. Confira a internet e rode de novo.' -ForegroundColor Yellow
    Stop-Script 1
}
