# 智论助手 · GitHub 首次推送脚本
# 用法：在项目根目录执行  powershell -ExecutionPolicy Bypass -File _push_github.ps1
#
# 前置条件：你已经在 GitHub 网页上建好了一个**空仓库**（不要勾 README / .gitignore / LICENSE）
#   https://github.com/new
#   仓库名建议：zhilun-assistant
#   可见性建议：Public（公开仓库的 Actions 分钟数免费；Private 也能用）

$ErrorActionPreference = "Stop"
$owner = "tiandaozongsi"
$repo  = "zhilun-assistant"
$url   = "https://github.com/$owner/$repo.git"

Write-Host "=== 1/5 检查当前分支与提交 ===" -ForegroundColor Cyan
$branch = (git rev-parse --abbrev-ref HEAD).Trim()
Write-Host "当前分支：$branch"
git log --oneline -1

Write-Host "`n=== 2/5 配置远端 origin ===" -ForegroundColor Cyan
$existing = (git remote 2>$null) -split "`n" | Where-Object { $_.Trim() -eq "origin" }
if ($existing) {
    git remote set-url origin $url
    Write-Host "已存在的 origin 已改为 $url"
} else {
    git remote add origin $url
    Write-Host "已添加 origin -> $url"
}
git remote -v

Write-Host "`n=== 3/5 统一分支名为 main ===" -ForegroundColor Cyan
if ($branch -ne "main") {
    git branch -M main
    Write-Host "分支已重命名为 main"
} else {
    Write-Host "分支已是 main"
}

Write-Host "`n=== 4/5 推送 ===" -ForegroundColor Cyan
Write-Host "首次推送会弹窗要求登录 GitHub。" -ForegroundColor Yellow
Write-Host "  用户名：$owner"
Write-Host "  密码：**不是账号密码**，要用 Personal Access Token (PAT)" -ForegroundColor Yellow
Write-Host "  生成方法：https://github.com/settings/tokens  ->  Generate new token (classic)"
Write-Host "            勾选 repo 权限，有效期选 90 天，生成后立刻复制（只显示一次）"
Write-Host ""
git push -u origin main

Write-Host "`n=== 5/5 完成 ===" -ForegroundColor Green
Write-Host "仓库地址：https://github.com/$owner/$repo"
Write-Host "别人克隆：git clone $url"
