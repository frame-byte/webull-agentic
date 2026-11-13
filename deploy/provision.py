"""Provision the gateway: DigitalOcean droplet (cloud-init) + cloud firewall + Cloudflare DNS-only A record.

  DO_TOKEN=... CF_TOKEN=... python3 deploy/provision.py --ssh-key-name my-key [--apply]

Without --apply it only prints the plan. Idempotent: reuses a droplet/firewall with the same
name and updates the A record in place. Standard library only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DO = "https://api.digitalocean.com/v2"
CF = "https://api.cloudflare.com/client/v4"
TRADINGVIEW_IPS = ["52.89.214.238", "34.212.75.30", "54.218.53.128", "52.32.178.7"]
HERE = Path(__file__).parent


def call(method: str, url: str, token: str, body=None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        sys.exit(f"{method} {url} -> HTTP {e.code}: {e.read().decode()[:500]}")
    return json.loads(raw) if raw else {}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ssh-key-name", required=True)
    ap.add_argument("--hostname", default="trade.mdtrade.app")
    ap.add_argument("--zone", default="mdtrade.app")
    ap.add_argument("--name", default="webull-gateway")
    ap.add_argument("--region", default="nyc1")  # s-1vcpu-1gb unavailable in nyc3 (Nov 2026)
    ap.add_argument("--size", default="s-1vcpu-1gb")
    ap.add_argument("--image", default="ubuntu-24-04-x64")
    ap.add_argument("--repo", default="frame-byte/webull-agentic")
    ap.add_argument("--branch", default="claude/clever-cray-qo2i0w")
    ap.add_argument("--ssh-cidr", default="0.0.0.0/0,::/0",
                    help="comma-separated CIDRs allowed to SSH (narrow to your home IP if it is static)")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    do_t, cf_t = os.environ.get("DO_TOKEN", ""), os.environ.get("CF_TOKEN", "")
    if not (do_t and cf_t):
        sys.exit("set DO_TOKEN and CF_TOKEN")

    keys = call("GET", f"{DO}/account/keys?per_page=200", do_t)["ssh_keys"]
    key = next((k for k in keys if k["name"] == a.ssh_key_name), None)
    if not key:
        sys.exit(f"SSH key {a.ssh_key_name!r} not found; have: {[k['name'] for k in keys]}")
    zones = call("GET", f"{CF}/zones?name={a.zone}", cf_t)["result"]
    if not zones:
        sys.exit(f"Cloudflare zone {a.zone} not visible to this token")
    zone_id = zones[0]["id"]

    user_data = ((HERE / "cloud-init.yaml").read_text()
                 .replace("__BRANCH__", a.branch).replace("__REPO__", a.repo).replace("__DOMAIN__", a.hostname)
                 .replace("__SSH_PUBKEY__", key["public_key"].strip()))
    assert "__" not in user_data.replace("__init__", ""), "unfilled placeholder in cloud-init"

    existing = [d for d in call("GET", f"{DO}/droplets?per_page=200", do_t)["droplets"] if d["name"] == a.name]
    print(f"plan: droplet {a.name} ({a.size}, {a.region}, {a.image}) key={key['name']} "
          f"{'[exists: reuse]' if existing else '[create]'}")
    print(f"plan: firewall {a.name}-fw: 22<-{a.ssh_cidr}, 80<-any (ACME), 443<-TradingView IPs only")
    print(f"plan: Cloudflare A {a.hostname} -> droplet IPv4, proxied=false (DNS only)")
    if not a.apply:
        print("dry plan only; rerun with --apply")
        return

    # 1. Droplet
    if existing:
        droplet = existing[0]
    else:
        droplet = call("POST", f"{DO}/droplets", do_t, {
            "name": a.name, "region": a.region, "size": a.size, "image": a.image,
            "ssh_keys": [key["id"]], "user_data": user_data, "monitoring": True,
            "ipv6": False, "backups": False, "tags": ["webull-gateway"],
        })["droplet"]
        print(f"created droplet {droplet['id']}")
    ip = None
    for _ in range(60):
        droplet = call("GET", f"{DO}/droplets/{droplet['id']}", do_t)["droplet"]
        ip = next((n["ip_address"] for n in droplet["networks"]["v4"] if n["type"] == "public"), None)
        if ip and droplet["status"] == "active":
            break
        time.sleep(5)
    if not ip:
        sys.exit("droplet did not get a public IPv4 in time")
    print(f"droplet {droplet['id']} active at {ip}")

    # 2. Cloud firewall
    ssh_src = [c.strip() for c in a.ssh_cidr.split(",") if c.strip()]
    fw_body = {
        "name": f"{a.name}-fw",
        "droplet_ids": [droplet["id"]],
        "inbound_rules": [
            {"protocol": "tcp", "ports": "22", "sources": {"addresses": ssh_src}},
            {"protocol": "tcp", "ports": "80", "sources": {"addresses": ["0.0.0.0/0", "::/0"]}},
            {"protocol": "tcp", "ports": "443", "sources": {"addresses": TRADINGVIEW_IPS}},
            {"protocol": "icmp", "sources": {"addresses": ["0.0.0.0/0", "::/0"]}},
        ],
        "outbound_rules": [
            {"protocol": p, "ports": "all" if p != "icmp" else None,
             "destinations": {"addresses": ["0.0.0.0/0", "::/0"]}} for p in ("tcp", "udp", "icmp")
        ],
    }
    for r in fw_body["outbound_rules"]:
        if r["ports"] is None:
            del r["ports"]
    fws = [f for f in call("GET", f"{DO}/firewalls?per_page=200", do_t)["firewalls"] if f["name"] == fw_body["name"]]
    if fws:
        call("PUT", f"{DO}/firewalls/{fws[0]['id']}", do_t, fw_body)
        print(f"updated firewall {fws[0]['id']}")
    else:
        fw = call("POST", f"{DO}/firewalls", do_t, fw_body)["firewall"]
        print(f"created firewall {fw['id']}")

    # 3. DNS-only A record
    rec = {"type": "A", "name": a.hostname, "content": ip, "ttl": 300, "proxied": False,
           "comment": "webull gateway droplet"}
    found = call("GET", f"{CF}/zones/{zone_id}/dns_records?type=A&name={a.hostname}", cf_t)["result"]
    if found:
        call("PUT", f"{CF}/zones/{zone_id}/dns_records/{found[0]['id']}", cf_t, rec)
        print(f"updated A {a.hostname} -> {ip}")
    else:
        call("POST", f"{CF}/zones/{zone_id}/dns_records", cf_t, rec)
        print(f"created A {a.hostname} -> {ip}")

    print(f"\ndone. ssh ubuntu@{ip}   (cloud-init takes ~3-5 min; check: cloud-init status --wait)")


if __name__ == "__main__":
    main()
