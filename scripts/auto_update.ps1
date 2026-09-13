# 每 10 分鐘更新一次開放資料，給 Windows 工作排程器用。
#
# GitHub Actions 的 schedule 對低活動量 repo 會大幅降級，實測 2～5 小時才跑一次。
# 這支在自己電腦上跑，間隔說十分鐘就是十分鐘——代價是電腦要開著。
#
# 註冊（PowerShell，不需要系統管理員）：
#
#   $a = New-ScheduledTaskAction -Execute "powershell.exe" `
#          -Argument '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "D:\程式\eco-warroom\scripts\auto_update.ps1"'
#   $t = New-ScheduledTaskTrigger -Once -At (Get-Date) `
#          -RepetitionInterval (New-TimeSpan -Minutes 10)
#   Register-ScheduledTask -TaskName "eco-warroom 更新開放資料" -Action $a -Trigger $t
#
# 停用： Unregister-ScheduledTask -TaskName "eco-warroom 更新開放資料" -Confirm:$false
# 看紀錄：Get-Content "$env:TEMP\eco-warroom-update.log" -Tail 20

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$log  = Join-Path $env:TEMP 'eco-warroom-update.log'

function Say($msg) {
    $line = "{0}  {1}" -f (Get-Date -Format 'MM-dd HH:mm:ss'), $msg
    Add-Content -Path $log -Value $line -Encoding utf8
}

try {
    Set-Location $repo

    # 先把遠端的變動拉下來，免得跟 Actions 自己跑的那次撞在一起
    git pull --rebase --quiet 2>$null

    # 用 py -3，不是 python——Windows 的 python 可能指到 Microsoft Store 的空殼
    $env:PYTHONIOENCODING = 'utf-8'
    py -3 scripts/fetch_data.py 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        # 四個來源全掛才會走到這裡；只掛一兩個仍然算成功
        Say "抓取失敗（exit $LASTEXITCODE），這次跳過"
        exit 0
    }

    git diff --quiet -- data.json
    if ($LASTEXITCODE -eq 0) { Say "data.json 沒有變動"; exit 0 }

    git add data.json
    git commit --quiet -m "chore: 更新開放資料 $(Get-Date -Format 'yyyy-MM-dd HH:mm')(本機排程)"
    git push --quiet
    Say "已更新並推送"
}
catch {
    # 沒網路、git 卡住之類的都不該讓排程整個停掉，記一筆就好
    Say "例外：$($_.Exception.Message)"
    exit 0
}
