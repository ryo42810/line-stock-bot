#!/usr/bin/env bash
# 配信履歴（history.json）と実行の記録（state.json）をリポジトリに保存する
set -eu
git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git add history.json state.json 2>/dev/null || git add history.json
if git diff --cached --quiet; then echo "保存するものはありません"; exit 0; fi
git commit -q -m "配信履歴を更新"
git pull -q --rebase --autostash origin "${GITHUB_REF_NAME:-main}"
git push -q origin "HEAD:${GITHUB_REF_NAME:-main}"
