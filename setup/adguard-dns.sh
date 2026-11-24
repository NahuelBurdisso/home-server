#!/bin/bash
# AdGuard Home's upstream DNS, made resilient. It had a single upstream (Quad9 over HTTPS) and no
# fallback, so every hiccup of Quad9 became a SERVFAIL for the whole house; and its default rate
# limit (20 queries/s) counts the whole home /24 as one client.
#   - two encrypted upstreams (Quad9 + Cloudflare): load_balance tries the faster first and the
#     other when it fails;
#   - plain 1.1.1.1 / 8.8.8.8 as the last resort when both fail;
#   - optimistic cache: an expired answer is served at once while it's refreshed, so a slow or
#     failing upstream doesn't stall browsing;
#   - no rate limit (port 53 only answers the LAN and the tailnet).
# AdGuard has no login (Tailscale is the login), so its API needs no password.
#   bash ~/server/setup/adguard-dns.sh
set -euo pipefail
API=http://127.0.0.1:3000/control

curl -fsS -X POST "$API/dns_config" -H 'Content-Type: application/json' -d '{
  "upstream_dns": ["https://dns10.quad9.net/dns-query", "https://cloudflare-dns.com/dns-query"],
  "upstream_mode": "load_balance",
  "bootstrap_dns": ["9.9.9.10", "149.112.112.10", "1.1.1.1", "1.0.0.1"],
  "fallback_dns": ["1.1.1.1", "8.8.8.8"],
  "upstream_timeout": 4,
  "cache_optimistic": true,
  "ratelimit": 0
}'
# Each upstream must answer, or the change made things worse.
curl -fsS -X POST "$API/test_upstream_dns" -H 'Content-Type: application/json' -d '{
  "upstream_dns": ["https://dns10.quad9.net/dns-query", "https://cloudflare-dns.com/dns-query"],
  "bootstrap_dns": ["9.9.9.10", "1.1.1.1"], "fallback_dns": ["1.1.1.1", "8.8.8.8"]
}' | jq .
curl -fsS "$API/dns_info" | jq -c '{upstream_dns, upstream_mode, fallback_dns, upstream_timeout, cache_optimistic, ratelimit}'
