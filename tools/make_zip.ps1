# 打包成 .mcdr（MCDR 多文件插件包）
#
# 用法（在 PlaytimeRecorder 目录下执行）：
#     powershell -ExecutionPolicy Bypass -File tools\make_zip.ps1
#
# 产物：dist\playtime_recorder-v<版本>.mcdr
# 安装：把 .mcdr 丢进 MCDR 的 plugins\ 目录即可。

$ErrorActionPreference = 'Stop'

$here = Split-Path -Parent $MyInvocation.MyCommand.Path       # ...\tools
$root = Split-Path -Parent $here                              # PlaytimeRecorder\

if (-not (Test-Path (Join-Path $root 'build.py'))) {
    Write-Error "在 $root 找不到 build.py"
    exit 1
}

Push-Location $root
try {
    python build.py
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} finally {
    Pop-Location
}
