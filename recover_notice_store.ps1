param(
    [string]$Root = "data/notices"
)

$ErrorActionPreference = "Stop"
$rootPath = (Resolve-Path -LiteralPath $Root).Path
$manifestPath = Join-Path $rootPath "manifest.json"
if (Test-Path -LiteralPath $manifestPath) {
    throw "Refusing to overwrite existing manifest: $manifestPath"
}

$entries = [ordered]@{}
foreach ($snapshot in Get-ChildItem -LiteralPath $rootPath -Filter "manifest-*.json" -File) {
    $document = Get-Content -Raw -LiteralPath $snapshot.FullName | ConvertFrom-Json
    foreach ($property in $document.partitions.PSObject.Properties) {
        if ($entries.Contains($property.Name)) {
            throw "Duplicate manifest entry: $($property.Name)"
        }
        $entries[$property.Name] = $property.Value
    }
}

$parquetFiles = @(Get-ChildItem -LiteralPath $rootPath -Filter "*.parquet" -Recurse -File)
if ($entries.Count -ne $parquetFiles.Count) {
    throw "Manifest/Parquet count mismatch: $($entries.Count) entries, $($parquetFiles.Count) files"
}

foreach ($parquet in $parquetFiles) {
    $month = $parquet.Directory.Name -replace "^month=", ""
    $year = $parquet.Directory.Parent.Name -replace "^year=", ""
    $layer = $parquet.Directory.Parent.Parent.Name
    $key = "$layer/$year-$month"
    if (-not $entries.Contains($key)) {
        throw "No manifest entry for Parquet partition: $key"
    }
    $canonicalPath = Join-Path $parquet.Directory.FullName "notices.parquet"
    if (Test-Path -LiteralPath $canonicalPath) {
        throw "Refusing to overwrite canonical partition: $canonicalPath"
    }
}

$lastSync = $entries.Values |
    ForEach-Object { [datetimeoffset]$_.updated_at } |
    Sort-Object -Descending |
    Select-Object -First 1
$manifest = [ordered]@{
    format_version = 1
    last_sync_at = $lastSync.ToString("o")
    partitions = $entries
}
$temporaryManifest = Join-Path $rootPath "manifest-recovered.json"
if (Test-Path -LiteralPath $temporaryManifest) {
    throw "Refusing to overwrite recovery file: $temporaryManifest"
}

try {
    [System.IO.File]::WriteAllText(
        $temporaryManifest,
        ($manifest | ConvertTo-Json -Depth 10),
        [System.Text.UTF8Encoding]::new($false)
    )
    foreach ($parquet in $parquetFiles) {
        $canonicalPath = Join-Path $parquet.Directory.FullName "notices.parquet"
        Move-Item -LiteralPath $parquet.FullName -Destination $canonicalPath
    }
    Move-Item -LiteralPath $temporaryManifest -Destination $manifestPath
} finally {
    if (Test-Path -LiteralPath $temporaryManifest) {
        Remove-Item -LiteralPath $temporaryManifest
    }
}

Write-Output "Recovered $($entries.Count) notice partitions"
