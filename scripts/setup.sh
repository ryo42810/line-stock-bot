#!/usr/bin/env bash
# 朝・夕方・動作確認で共通の準備（日本語フォントとライブラリ）
set -u
# フォントの取得は、GitHub側の一時的な通信エラーで失敗することがあるので3回まで試す。
# それでも入らなければ画像の文字が化けるだけなので、配信自体は止めない
for i in 1 2 3; do
  if sudo apt-get update -qq && sudo apt-get install -y -qq fonts-noto-cjk > /dev/null; then
    echo "フォントを入れました"; break
  fi
  echo "フォントの取得に失敗（${i}回目）"; sleep 20
done
set -e
pip install -q -r requirements.txt
