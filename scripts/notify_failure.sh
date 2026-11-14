#!/usr/bin/env bash
# Pythonが動く前に失敗したとき（インストール失敗など）もLINEに知らせる。
# USER_ID に改行や空白が混ざっていても送れるように、取り除いてからJSONを組み立てる
set -eu
to=$(printf '%s' "${USER_ID}" | tr -d '[:space:]')
token=$(printf '%s' "${LINE_ACCESS_TOKEN}" | tr -d '[:space:]')
body=$(jq -n --arg to "$to" --arg text "$1" '{to: $to, messages: [{type: "text", text: $text}]}')
curl -sS --fail-with-body -X POST https://api.line.me/v2/bot/message/push \
  -H "Content-Type: application/json" -H "Authorization: Bearer ${token}" -d "$body"
