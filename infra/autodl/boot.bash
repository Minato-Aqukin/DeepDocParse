#!/usr/bin/env bash
# 开机自启动入口：实例开机后把整站拉起来（= 没人登录时自动跑一次 `stack.bash up`）。
#
# AutoDL 没有 systemd，平台也不替你拉进程；关机再开或从镜像开出新实例，进程一个都不在。
# 怎么挂上开机这一刻见 README「开机自启动」。
#
# 开机时执行命令的环境近乎是空的（没有 HOME，PATH 只有寥寥几项 —— stack.bash 在 `set -u`
# 下读 $HOME 会直接退出），执行方也不保证会等它跑完（模型线要几分钟才就绪）。所以这里只做三件事：
# 补齐环境、立刻脱离调用方（setsid -f）、把全部输出落到 $LOG_DIR/boot.log。
# 真正的活交给 `stack.bash up`：它自带串行锁，开机这一份与手敲的 up 撞在一起也安全。
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export HOME="${HOME:-/root}"
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$HOME/.local/bin${PATH:+:$PATH}"

if [ -z "${DDP_BOOT_DETACHED:-}" ]; then
  DDP_BOOT_DETACHED=1 setsid -f bash "${BASH_SOURCE[0]}" </dev/null >/dev/null 2>&1
  exit 0
fi

# shellcheck source=root.bash
source "$HERE/root.bash"
LOG_DIR="${LOG_DIR:-$DDP_ROOT/logs}"
mkdir -p "$LOG_DIR"
exec >>"$LOG_DIR/boot.log" 2>&1

echo "==== $(date '+%F %T') 开机自启动"
bash "$HERE/stack.bash" up
rc=$?
echo "==== $(date '+%F %T') 结束，退出码 $rc"
