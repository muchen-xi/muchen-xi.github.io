#!/usr/bin/env bash
# =============================================================================
# net-watchdog — 树莓派网络看门狗（无人值守节点必备）
#
# 背景（2026-09-24 实测）：这块 Zero W 的 BCM43438（WiFi+BT 二合一）会周期性卡死——
#   开机后网络正常几分钟，随后「链路仍关联、信号 -35dBm、但 DNS/流量全不通」，
#   只能靠人工拔电恢复。内核 brcmfmac 无报错，属射频/固件层卡死。
#   看门狗把「手动拔电」变成「自愈」，按三级处置逐级升级：
#     ① 连续 2 次不通（约 4 分钟）→ 重置 WiFi 连接（nmcli）
#     ② 连续 4 次不通（约 8 分钟）→ 重启 NetworkManager
#     ③ 连续 6 次不通（约 12 分钟）→ 整机重启（带 1 小时冷却，避免重启风暴）
#
# 用法: net-watchdog.sh          （由 net-watchdog.timer 每 2 分钟调用一次）
# 日志: /var/lib/net-watchdog/state.log（自动截断到 2000 行）
# =============================================================================
set -u

STATE_DIR="/var/lib/net-watchdog"
FAIL_FILE="$STATE_DIR/fails"
REBOOT_FILE="$STATE_DIR/last_reboot"
LOG_FILE="$STATE_DIR/state.log"
DNS_PROBE="223.5.5.5"
CON_NAME="preconfigured"          # NetworkManager 连接名（树莓派默认）
MAX_LINES=2000

mkdir -p "$STATE_DIR"
log() {
    printf '%s %s\n' "$(date '+%F %T')" "$*" >> "$LOG_FILE"
    # 控制体积：超过上限只保留最后 MAX_LINES 行
    if [ "$(wc -l < "$LOG_FILE" 2>/dev/null || echo 0)" -gt "$MAX_LINES" ]; then
        tail -n "$MAX_LINES" "$LOG_FILE" > "${LOG_FILE}.tmp" 2>/dev/null && mv "${LOG_FILE}.tmp" "$LOG_FILE"
    fi
}

fails=0
[ -f "$FAIL_FILE" ] && fails=$(cat "$FAIL_FILE" 2>/dev/null || echo 0)

# ── 连通性判定（三项分别记录，便于区分故障层级）──
#   lan: 默认网关可达（局域网链路/路由是否还在）
#   wan: 直接 ping 中立 IP（不看 DNS，验证出网路由）
#   dns: 走系统解析器解析一个中立域名（= agent 真实要用的路径）
gw=$(ip route show default 2>/dev/null | awk '{print $3; exit}')
lan_ok=0
[ -n "$gw" ] && ping -c 2 -W 2 -n "$gw" >/dev/null 2>&1 && lan_ok=1
wan_ok=0
ping -c 1 -W 3 -n 223.5.5.5 >/dev/null 2>&1 && wan_ok=1
dns_ok=0
timeout 8 python3 -c "import socket,sys; socket.setdefaulttimeout(5); socket.getaddrinfo('www.baidu.com',443)" >/dev/null 2>&1 && dns_ok=1

if [ "$lan_ok" = 1 ] && [ "$wan_ok" = 1 ] && [ "$dns_ok" = 1 ]; then
    if [ "$fails" != "0" ]; then log "✅ 网络恢复（此前连续失败 $fails 次）"; fi
    echo 0 > "$FAIL_FILE"
    exit 0
fi

fails=$((fails + 1))
echo "$fails" > "$FAIL_FILE"
log "⚠ 网络不可达（第 $fails 次）：网关=${gw:-无} LAN=${lan_ok} WAN=${wan_ok} DNS=${dns_ok}"

case "$fails" in
    2)
        log "→ ① 重置 WiFi 连接（nmcli con up $CON_NAME）"
        nmcli con up "$CON_NAME" >/dev/null 2>&1 || nmcli device reconnect wlan0 >/dev/null 2>&1 || true
        ;;
    4)
        log "→ ② 重启 NetworkManager"
        systemctl restart NetworkManager >/dev/null 2>&1 || true
        ;;
    6)
        last=0
        [ -f "$REBOOT_FILE" ] && last=$(cat "$REBOOT_FILE" 2>/dev/null || echo 0)
        now=$(date +%s)
        if [ $((now - last)) -ge 3600 ]; then
            log "→ ③ 网络持续不可达 12 分钟，整机重启（冷却 1 小时）"
            echo "$now" > "$REBOOT_FILE"
            sync
            (sleep 2; systemctl reboot) &
        else
            log "→ ③ 已触发过整机重启（$(( (now - last) / 60 )) 分钟前），本次跳过以免重启风暴"
        fi
        ;;
esac
exit 1
