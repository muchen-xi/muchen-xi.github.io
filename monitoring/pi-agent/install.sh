#!/usr/bin/env bash
# =============================================================================
# dr-agent 一键安装（树莓派 / Raspberry Pi OS）
#
#   sudo bash install.sh                 # 交互式录入 AK 与 SMTP（首次）
#   sudo bash install.sh --reset-config  # 已存在配置时重新录入（覆盖旧配置）
#
# 非交互安装（CI / 批量）—— 先 export 变量，再用 sudo -E 透传：
#   export ALI_KEY_ID=xxx ALI_KEY_SECRET=yyy SMTP_USERNAME=ops@example.com
#   export SMTP_PASSWORD=zzz REPORT_TO=me@example.com
#   sudo -E bash install.sh
# 或者：sudo env ALI_KEY_ID=xxx ALI_KEY_SECRET=yyy bash install.sh
#
# 幂等：重复执行只更新 /opt/dr-agent/dr_agent.py 与 systemd unit，
#       已存在的 /etc/dr-agent.env 默认原样保留（不会覆盖 AK / SMTP）。
# =============================================================================
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
APP_DIR="/opt/dr-agent"
STATE_DIR="/var/lib/dr-agent"
CONF_FILE="/etc/dr-agent.env"
SERVICE_FILE="/etc/systemd/system/dr-agent.service"
LOG_FILE="${STATE_DIR}/dr-agent.log"
PYBIN="/usr/bin/python3"
RESET_CONFIG=0

for arg in "$@"; do
    case "$arg" in
        --reset-config) RESET_CONFIG=1 ;;
        -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
        *) echo "❌ 未知参数: $arg（支持 --reset-config）" >&2; exit 2 ;;
    esac
done

log()  { printf '%s\n' "$*"; }
warn() { printf '⚠ %s\n' "$*" >&2; }
die()  { printf '❌ %s\n' "$*" >&2; exit 1; }

# ─────────────────────────── 1. 前置检查 ───────────────────────────
[[ "${EUID:-$(id -u)}" -eq 0 ]] || die "请用 root 运行: sudo bash install.sh"

command -v systemctl >/dev/null 2>&1 || die "未找到 systemctl（本安装脚本面向 systemd 发行版）"
[[ -f "${SRC_DIR}/dr_agent.py" ]] || die "同目录缺少 dr_agent.py（请完整拷贝 pi-agent/ 目录）"
[[ -f "${SRC_DIR}/dr-agent.service" ]] || die "同目录缺少 dr-agent.service"

command -v python3 >/dev/null 2>&1 || die "未找到 python3（sudo apt install python3）"
PYBIN="$(command -v python3)"
if ! "$PYBIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
    die "需要 Python ≥ 3.9，当前 $("$PYBIN" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
fi
log "✅ Python: $("$PYBIN" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])') ($PYBIN)"

if command -v curl >/dev/null 2>&1; then
    log "✅ curl: $(command -v curl)"
else
    warn "未找到 curl — dr-agent 会回退 stdlib ssl+socket 手写探测（可用但建议 sudo apt install curl）"
fi

# ─────────────────────────── 2. 目录与程序文件 ───────────────────────────
install -d -m 0755 "$APP_DIR" "$STATE_DIR"
install -m 0755 "${SRC_DIR}/dr_agent.py" "${APP_DIR}/dr_agent.py"
for extra in README.md config.env.example; do
    if [[ -f "${SRC_DIR}/${extra}" ]]; then
        install -m 0644 "${SRC_DIR}/${extra}" "${APP_DIR}/${extra}"
    fi
done
log "✅ 程序已安装: ${APP_DIR}/dr_agent.py"

# ─────────────────────────── 3. 配置文件（幂等，chmod 600） ───────────────────────────
read_value() {  # $1=环境变量名 $2=提示 $3=默认值 $4=1 表示密码（隐藏输入）
    local var="$1" prompt="$2" default="${3:-}" secret="${4:-0}" value=""
    value="${!var:-}"
    if [[ -z "$value" && -t 0 ]]; then
        if [[ "$secret" == "1" ]]; then
            read -r -s -p "${prompt}${default:+ [$default]}: " value || true
            echo
        else
            read -r -p "${prompt}${default:+ [$default]}: " value || true
        fi
    fi
    [[ -n "$value" ]] || value="$default"
    printf -v "$var" '%s' "$value"
}

write_config() {
    local ak_id="$1" ak_secret="$2" smtp_user="$3" smtp_pass="$4" report_to="$5"
    local smtp_server="${SMTP_SERVER:-smtp.qiye.aliyun.com}"
    local smtp_port="${SMTP_PORT:-465}"
    local sender_name="${SMTP_SENDER_NAME:-晨曦的宇宙 · 树莓派观察者}"
    local role="${DR_ROLE:-switch_only}"
    # 用 python 写文件：正确转义引号/反斜杠，避免密码里的 $ ` " 破坏配置
    DR_OUT_AK_ID="$ak_id" DR_OUT_AK_SECRET="$ak_secret" \
    DR_OUT_SMTP_USER="$smtp_user" DR_OUT_SMTP_PASS="$smtp_pass" \
    DR_OUT_REPORT_TO="$report_to" DR_OUT_SMTP_SERVER="$smtp_server" \
    DR_OUT_SMTP_PORT="$smtp_port" DR_OUT_SENDER_NAME="$sender_name" DR_OUT_ROLE="$role" \
    "$PYBIN" - "$CONF_FILE" <<'PY'
import os
import sys

path = sys.argv[1]


def q(value):
    """systemd EnvironmentFile 双引号值 + C 风格转义（dr_agent 解析端会还原）。"""
    return '"%s"' % str(value).replace("\\", "\\\\").replace('"', '\\"')


lines = [
    "# dr-agent 配置 — 树莓派容灾观察者（契约 monitoring/DR-OBSERVER-CONTRACT.md）",
    "# 权限必须 600；修改后 sudo systemctl restart dr-agent",
    "",
    "# ── 阿里云 DNS 凭据（与仓库其它脚本同套 secrets） ──",
    "ALI_KEY_ID=%s" % q(os.environ.get("DR_OUT_AK_ID", "")),
    "ALI_KEY_SECRET=%s" % q(os.environ.get("DR_OUT_AK_SECRET", "")),
    "ALI_REGION=cn-hangzhou",
    "",
    "# ── 运行角色与目标 ──",
    "# switch_only = 阶段一：只执行切换，恢复判定只记录+告警；full = 阶段二：按契约第四节执行恢复",
    "DR_ROLE=%s" % q(os.environ.get("DR_OUT_ROLE", "switch_only")),
    "DR_TARGETS=www,starkeeper",
    "",
    "# ── 节奏（秒） ──",
    "DR_TICK_SECONDS=30",
    "DR_FULL_PROBE_SECONDS=300",
    "DR_FAST_PROBE_SECONDS=30",
    "",
    "# ── 阈值 ──",
    "DR_TCP_FAILS_TO_ESCALATE=2",
    "DR_FAILS_TO_SWITCH=3",
    "DR_STREAK_TO_RESTORE=3",
    "DR_MIN_DWELL_SECONDS=1800",
    "DR_PEER_MAX_AGE_SECONDS=1200",
    "DR_CLOCK_SKEW_MAX=300",
    "DR_CLOCK_CHECK_SECONDS=1800",
    "",
    "# ── 告警 ──",
    "DR_ALERT_ENABLED=1",
    "DR_DRY_RUN=0",
    "SMTP_SERVER=%s" % q(os.environ.get("DR_OUT_SMTP_SERVER", "smtp.qiye.aliyun.com")),
    "SMTP_PORT=%s" % q(os.environ.get("DR_OUT_SMTP_PORT", "465")),
    "SMTP_USERNAME=%s" % q(os.environ.get("DR_OUT_SMTP_USER", "")),
    "SMTP_PASSWORD=%s" % q(os.environ.get("DR_OUT_SMTP_PASS", "")),
    "SMTP_SENDER_NAME=%s" % q(os.environ.get("DR_OUT_SENDER_NAME", "晨曦的宇宙 · 树莓派观察者")),
    "REPORT_TO=%s" % q(os.environ.get("DR_OUT_REPORT_TO", "")),
    "",
]
with open(path, "w", encoding="utf-8") as f:
    f.write("\n".join(lines))
PY
    chmod 600 "$CONF_FILE"
    log "✅ 配置已写入: ${CONF_FILE}（chmod 600）"
}

if [[ -f "$CONF_FILE" && "$RESET_CONFIG" -eq 0 ]]; then
    chmod 600 "$CONF_FILE" || true
    log "ℹ 已存在 ${CONF_FILE} — 保留现有配置（如需重新录入请加 --reset-config）"
else
    if [[ "$RESET_CONFIG" -eq 1 && -f "$CONF_FILE" ]]; then
        cp -a "$CONF_FILE" "${CONF_FILE}.bak.$(date +%Y%m%d%H%M%S)"
        log "ℹ 已备份旧配置到 ${CONF_FILE}.bak.*"
    fi
    if [[ -t 0 ]]; then
        log "── 录入阿里云凭据（留空则稍后手工编辑 ${CONF_FILE}） ──"
        read_value ALI_KEY_ID "阿里云 AccessKey ID" "" 0
        read_value ALI_KEY_SECRET "阿里云 AccessKey Secret" "" 1
        log "── 录入告警邮箱（可留空，稍后手工编辑） ──"
        read_value SMTP_USERNAME "SMTP 账号（发件）" "" 0
        read_value SMTP_PASSWORD "SMTP 密码/授权码" "" 1
        read_value REPORT_TO "告警收件人（多个用英文逗号分隔）" "" 0
    else
        warn "非交互模式：使用已 export 的环境变量（ALI_KEY_ID / ALI_KEY_SECRET / SMTP_* / REPORT_TO）"
    fi
    write_config "${ALI_KEY_ID:-}" "${ALI_KEY_SECRET:-}" \
                 "${SMTP_USERNAME:-}" "${SMTP_PASSWORD:-}" "${REPORT_TO:-}"
    if [[ -z "${ALI_KEY_ID:-}" || -z "${ALI_KEY_SECRET:-}" ]]; then
        warn "AK 未录入完整 — 请编辑 ${CONF_FILE} 后再 sudo systemctl restart dr-agent"
        warn "缺失 AK 时 --loop 会拒绝启动（这是有意的保护）"
    fi
fi

# ─────────────────────────── 4. systemd unit ───────────────────────────
install -m 0644 "${SRC_DIR}/dr-agent.service" "$SERVICE_FILE"
log "✅ systemd unit 已安装: ${SERVICE_FILE}"

systemctl daemon-reload
if systemctl enable --now dr-agent.service; then
    log "✅ dr-agent 已设为开机自启并启动"
else
    warn "systemctl enable --now 失败 — 请检查: systemctl status dr-agent"
fi

# ─────────────────────────── 5. 收尾提示 ───────────────────────────
echo
systemctl --no-pager --full status dr-agent.service 2>/dev/null | head -n 12 || true
cat <<EOF

✅ 安装完成。常用命令：
  自检:   sudo ${PYBIN} ${APP_DIR}/dr_agent.py --selftest
  状态:   sudo ${PYBIN} ${APP_DIR}/dr_agent.py --status
  单轮:   sudo ${PYBIN} ${APP_DIR}/dr_agent.py --once --dry-run
  日志:   journalctl -u dr-agent -f        （或 tail -f ${LOG_FILE}）
  配置:   sudo nano ${CONF_FILE}            （改完 sudo systemctl restart dr-agent）
  停止:   sudo systemctl stop dr-agent     （回滚/排障时先停掉，见 README）
EOF
