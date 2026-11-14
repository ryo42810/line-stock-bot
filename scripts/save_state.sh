#!/usr/bin/env bash
# 配信履歴（history.json）と実行の記録（state.json / topics_state.json / budget/ の使用額）をリポジトリに保存する
set -eu
git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
for f in history.json state.json topics_state.json budget/*.json; do
  [ -f "$f" ] && git add "$f"
done
if git diff --cached --quiet; then echo "保存するものはありません"; exit 0; fi
git commit -q -m "配信履歴を更新"
# 朝・夕方・ネタ候補が同時に保存しようとすると先を越されることがあるので、3回まで試す
for i in 1 2 3; do
  git pull -q --rebase --autostash origin "${GITHUB_REF_NAME:-main}"
  if git push -q origin "HEAD:${GITHUB_REF_NAME:-main}"; then exit 0; fi
  echo "保存に失敗（${i}回目）。少し待ってやり直します"; sleep $((i * 5))
done
exit 1
