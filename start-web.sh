#!/bin/zsh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
if [[ ! -x .venv/bin/xuns ]]; then
  echo "未找到 .venv。请在仓库根目录执行: uv venv --python 3.13 && uv pip install -e . pyyaml" >&2
  exit 1
fi
mkdir -p .xun workspace
if [[ ! -s .xun/web.token ]]; then
  .venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(18))' > .xun/web.token
  chmod 600 .xun/web.token
fi
TOKEN="$(cat .xun/web.token)"
export PYTHONUNBUFFERED=1
# 浏览器装在用户缓存里；避免继承临时目录里的 PLAYWRIGHT_BROWSERS_PATH
unset PLAYWRIGHT_BROWSERS_PATH
exec .venv/bin/xuns workspace --host 127.0.0.1 --port 18960 --token "$TOKEN"
