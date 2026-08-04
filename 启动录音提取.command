#!/bin/bash
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_PY="$SCRIPT_DIR/.venv/bin/python"
APP_PY="$SCRIPT_DIR/app.py"
LOG_FILE="$SCRIPT_DIR/launcher_error.log"

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/opt/local/bin:$PATH"

if [ ! -x "$VENV_PY" ]; then
  osascript -e 'display alert "未找到 Python 虚拟环境" message "请先在 luyintiqu 目录执行依赖安装。" as critical'
  exit 1
fi

"$VENV_PY" "$APP_PY" 2>"$LOG_FILE"
exit_code=$?
if [ $exit_code -ne 0 ]; then
  osascript -e "display alert \"启动失败\" message \"请查看 $LOG_FILE\" as critical"
fi
exit $exit_code
