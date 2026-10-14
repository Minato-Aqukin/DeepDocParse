#!/usr/bin/env bash
# AutoDL 部署的**安装根目录**与**实例级配置**。env.bash（模型线）与 stack.bash（应用面）都 source 它，
# 两边对"装在哪、这台机器怎么配"只有这一份答案。
#
# ## 为什么一律落系统盘（/root/ddp）
#
# AutoDL 的「保存镜像」**只打包系统盘**；普通实例的数据盘 `/root/autodl-tmp` 不进镜像
# （官方文档：数据盘"不能保存至镜像中"）。旧缺省是"有数据盘就用数据盘"，于是 venv、
# 权重、stack.env 里的全部密钥、MinIO 数据、隧道令牌都躺在数据盘上 —— 存出来的镜像里
# 只剩 PostgreSQL（/var/lib/postgresql 在系统盘）：库里的口令对不上新生成的 stack.env，
# 节点身份文件也不见了，从镜像开出来的实例起不来，而存镜像那一步不报任何错。
# （Pro 实例没有独立数据盘，`/root/autodl-tmp` 只是系统盘上的目录、会进镜像；放 /root/ddp
# 才让两种实例的行为一致。）
#
# 旧部署要先搬家：`mv /root/autodl-tmp/ddp /root/ddp`，venv 再按 README「从数据盘迁到系统盘」
# 改写解释器路径。下面那道闸就是拦"没搬家就用新缺省"的：那样 install 会在空目录里
# 生成一份新的 stack.env，与库里已有的角色口令对不上。

DDP_ROOT="${DDP_ROOT:-/root/ddp}"

if [ "$DDP_ROOT" = /root/ddp ] && [ ! -e /root/ddp ] && [ -d /root/autodl-tmp/ddp ]; then
  echo "[autodl] 发现旧部署 /root/autodl-tmp/ddp（数据盘，不进镜像），而 /root/ddp 不存在。" >&2
  echo "[autodl] 先搬到系统盘：mv /root/autodl-tmp/ddp /root/ddp（venv 路径改写见 infra/autodl/README.md）" >&2
  echo "[autodl] 确实要留在原处就显式 export DDP_ROOT=/root/autodl-tmp/ddp —— 那样存镜像会丢掉它。" >&2
  exit 1
fi

# ---- 平台注入的线程数 ----
# AutoDL 把 OMP_NUM_THREADS / MKL_NUM_THREADS 写进容器环境（PID 1、开机钩子、SSH 会话全都继承）。
# 2026-10-14 实测：无卡模式开过一次之后它是 0，紧接着那次有卡开机（cgroup 配额 16 核）也还是 0；
# 再下一次有卡开机又是 16 —— 平台行为不确定，所以每次都检查。
# vLLM 拿它去 torch.set_num_threads(0)，OCR 当场崩（"set_num_threads expects a positive integer"），
# 指令模型跟着等满 10 分钟，网站和隧道排在后面迟迟起不来。
# 只修坏值（0、空、非数字）：按 cgroup 实际 CPU 配额重算，至少 1；没设置的保持没设置。
_ddp_cpu_quota() {
  local quota period
  if { read -r quota period < /sys/fs/cgroup/cpu.max; } 2>/dev/null; then :
  else
    quota="$(cat /sys/fs/cgroup/cpu/cpu.cfs_quota_us 2>/dev/null)"
    period="$(cat /sys/fs/cgroup/cpu/cpu.cfs_period_us 2>/dev/null)"
  fi
  case "$quota:$period" in
    max:*|-1:*|:*|*:) nproc ;;
    *) echo $(( quota / period > 0 ? quota / period : 1 )) ;;
  esac
}
for _var in OMP_NUM_THREADS MKL_NUM_THREADS; do
  if [ -n "${!_var+x}" ]; then
    case "${!_var}" in ''|*[!0-9]*|0|00*) export "$_var=$(_ddp_cpu_quota)" ;; esac
  fi
done
unset _var

# ---- 实例级配置：$DDP_ROOT/deploy.env ----
# 这台机器自己的取值（公网域名、游客模式、注册表、要不要起模型线……）。
# 它在系统盘上，跟着镜像走：从镜像开出来的实例 `stack.bash up` 一条命令就回到原样，
# 不用记当初 export 过什么。
#
# 格式是一行一个 `KEY=VALUE`（不做 shell 展开、不支持引号，注释只能独占一行 —— 行尾 `# …`
# 会成为值的一部分）。**外部已 export 的同名变量优先**，与各脚本里 `${VAR:-默认值}` 的约定
# 一致 —— 临时覆盖一项不需要改文件。
DEPLOY_ENV="$DDP_ROOT/deploy.env"
if [ -f "$DEPLOY_ENV" ]; then
  while IFS= read -r _line || [ -n "$_line" ]; do
    case "$_line" in ''|'#'*) continue ;; esac
    _key="${_line%%=*}"
    [[ "$_key" =~ ^[A-Z_][A-Z0-9_]*$ ]] || { echo "[autodl] 忽略 $DEPLOY_ENV 里不合法的一行：$_line" >&2; continue; }
    [ -n "${!_key+x}" ] && continue
    export "$_key=${_line#*=}"
  done < "$DEPLOY_ENV"
  unset _line _key
fi
