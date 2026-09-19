[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('linux/arm64', 'linux/amd64')]
    [string]$Platform
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$architecture = $Platform.Replace('linux/', '')
$imageTag = "yandex-calendar-bot:build-$architecture"
$exportTag = 'yandex-calendar-bot:latest'
$outputDirectory = Join-Path $projectRoot 'dist'
$archivePath = Join-Path $outputDirectory "yandex-calendar-bot-$architecture.tar"
$smokeScript = Join-Path $PSScriptRoot 'smoke_test.py'

New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null

docker buildx build --platform $Platform --pull --load --tag $imageTag $projectRoot
if ($LASTEXITCODE -ne 0) { throw 'Docker image build failed.' }

$actualPlatform = docker image inspect --format '{{.Os}}/{{.Architecture}}' $imageTag
if ($LASTEXITCODE -ne 0 -or $actualPlatform.Trim() -ne $Platform) {
    throw "Unexpected image platform: $actualPlatform"
}

# No credentials and no external network; verify that a recreated container sees its SQLite data.
$testVolume = 'caldavbot-smoke-' + [guid]::NewGuid().ToString('N')
docker volume create $testVolume | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Cannot create the temporary smoke-test volume.' }
try {
    foreach ($mode in @('empty', 'existing')) {
        Get-Content -Raw -Encoding UTF8 -LiteralPath $smokeScript |
            docker run --rm -i --platform $Platform --network none --read-only `
                --tmpfs /tmp:rw,nosuid,noexec,size=16m `
                --cap-drop ALL --security-opt no-new-privileges:true `
                --mount "type=volume,src=$testVolume,dst=/data" `
                $imageTag python - $mode
        if ($LASTEXITCODE -ne 0) { throw "Container smoke test failed: $mode" }
    }
}
finally {
    # This volume was created above solely for the smoke tests; it contains no user data.
    docker volume rm $testVolume | Out-Null
}

docker image tag $imageTag $exportTag
if ($LASTEXITCODE -ne 0) { throw 'Cannot tag the verified image.' }
docker image save --output $archivePath $exportTag
if ($LASTEXITCODE -ne 0) { throw 'Cannot export the image archive.' }

$archiveHash = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()
$checksumLine = "$archiveHash  $([IO.Path]::GetFileName($archivePath))`n"
[IO.File]::WriteAllText("$archivePath.sha256", $checksumLine, [Text.UTF8Encoding]::new($false))
Write-Host "Ready: $archivePath"
Write-Host "Platform: $Platform"
Write-Host "SHA256: $archiveHash"
