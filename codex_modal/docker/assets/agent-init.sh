#!/bin/sh
# Root-stage init for the codex-modal Docker sandbox.
#
# The container is already on a Docker `internal` network, so it has no route to
# the internet, to the host, or to the LAN. This adds a second, independent
# layer: a default-deny egress firewall that permits nothing except the egress
# broker. CAP_NET_ADMIN is scoped to this container's own network namespace, so
# these rules cannot touch host networking. Privileges are dropped before Codex
# starts.
set -eu

PROXY_IP="${SANDBOX_PROXY_IP:?SANDBOX_PROXY_IP is required}"
PROXY_PORT="${SANDBOX_PROXY_PORT:-3128}"
MODEL_PORT="${SANDBOX_MODEL_PORT:-8081}"
FIREWALL="${SANDBOX_FIREWALL:-enforce}"

pick_iptables() {
    for candidate in iptables-legacy iptables iptables-nft; do
        if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -L -n >/dev/null 2>&1; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

apply_firewall() {
    ipt="$1"
    # Return traffic from the broker plus loopback (Docker's embedded resolver
    # lives on 127.0.0.11) is all the sandbox is allowed to exchange. Rejecting
    # rather than dropping keeps blocked calls fast instead of hanging.
    "$ipt" -P FORWARD DROP || return 1
    "$ipt" -F OUTPUT || return 1
    "$ipt" -F INPUT || return 1
    "$ipt" -A OUTPUT -o lo -j ACCEPT || return 1
    "$ipt" -A INPUT -i lo -j ACCEPT || return 1
    "$ipt" -A OUTPUT -d "$PROXY_IP" -p tcp --dport "$PROXY_PORT" -j ACCEPT || return 1
    "$ipt" -A OUTPUT -d "$PROXY_IP" -p tcp --dport "$MODEL_PORT" -j ACCEPT || return 1
    "$ipt" -A INPUT -s "$PROXY_IP" -p tcp -j ACCEPT || return 1
    "$ipt" -A OUTPUT -p tcp -j REJECT --reject-with tcp-reset || return 1
    "$ipt" -A OUTPUT -j REJECT || return 1
    "$ipt" -A INPUT -j DROP || return 1
    "$ipt" -P OUTPUT DROP || return 1
    "$ipt" -P INPUT DROP || return 1
    return 0
}

apply_firewall6() {
    ipt6="$1"
    "$ipt6" -F OUTPUT 2>/dev/null || true
    "$ipt6" -F INPUT 2>/dev/null || true
    "$ipt6" -A OUTPUT -o lo -j ACCEPT 2>/dev/null || true
    "$ipt6" -A INPUT -i lo -j ACCEPT 2>/dev/null || true
    "$ipt6" -P OUTPUT DROP 2>/dev/null || true
    "$ipt6" -P INPUT DROP 2>/dev/null || true
    "$ipt6" -P FORWARD DROP 2>/dev/null || true
}

fail_firewall() {
    echo "codex-sandbox: $1" >&2
    if [ "$FIREWALL" = "enforce" ]; then
        echo "codex-sandbox: refusing to start. Re-run with --docker-firewall warn" \
             "to accept Docker-network-only isolation." >&2
        exit 70
    fi
}

if [ "$FIREWALL" = "off" ]; then
    echo "codex-sandbox: egress firewall disabled by request; network isolation" \
         "relies on the Docker internal network alone." >&2
else
    set +e
    ipt=$(pick_iptables)
    picked=$?
    set -e
    if [ "$picked" -ne 0 ]; then
        fail_firewall "no usable iptables backend (needs --cap-add NET_ADMIN)."
    else
        set +e
        apply_firewall "$ipt"
        applied=$?
        set -e
        if [ "$applied" -ne 0 ]; then
            fail_firewall "failed to apply the egress firewall (exit ${applied})."
        else
            for candidate in ip6tables-legacy ip6tables ip6tables-nft; do
                if command -v "$candidate" >/dev/null 2>&1; then
                    apply_firewall6 "$candidate"
                    break
                fi
            done
            echo "codex-sandbox: egress locked to ${PROXY_IP}:${PROXY_PORT},${MODEL_PORT} via ${ipt}." >&2
        fi
    fi
fi

# Package managers reach the network only through the broker. sudo resets the
# environment, so bake the proxy into apt's own config rather than relying on
# inherited variables. DNS is resolved broker-side.
if [ "$FIREWALL" != "off" ]; then
    cat > /etc/apt/apt.conf.d/01codex-proxy <<EOF
Acquire::http::Proxy "http://${PROXY_IP}:${PROXY_PORT}";
Acquire::https::Proxy "http://${PROXY_IP}:${PROXY_PORT}";
EOF
fi

# npm installs globally into a user-writable prefix so the agent never needs root
# just to add a CLI. Created here because /home/agent is a fresh per-run volume
# whose contents are owned by root until we hand it to the agent below.
mkdir -p /home/agent/.npm-global /home/agent/.npm /home/agent/go 2>/dev/null || true

# The host copies the run spec and this package into /sandbox as root. Make it
# readable but not writable by the unprivileged user Codex will run as.
chown -R root:root /sandbox 2>/dev/null || true
chmod -R a+rX,go-w /sandbox 2>/dev/null || true

if [ -d /sandbox/copy-in ]; then
    cp -a /sandbox/copy-in/. /work/ 2>/dev/null || true
fi
chown -R agent:agent /work /sandbox-state /home/agent 2>/dev/null || true

exec gosu agent python3 -m codex_modal.container_entry "$@"
