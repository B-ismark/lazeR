# LazeR rendezvous — deploy

The rendezvous is a tiny, stateless UDP coordinator that lets a phone and a saved
laptop find each other **across networks** (off the same LAN) and, when a NAT
refuses to be punched, relays their still-encrypted packets. It never sees your
AES key and can't drive or decrypt a laptop — see [../PROTOCOL.md](../PROTOCOL.md).

One `python3 rendezvous_server.py` process. Stdlib only, no dependencies. It needs
a box with a **public IPv4** and one open **UDP** port (default `50510`).

## Where to host (cheapest → easy)

| Option | Cost | Notes |
|---|---|---|
| **Oracle Cloud Always Free (ARM VM)** | **$0 forever** | Public IPv4, 10 TB/mo egress — enough for relay. Best value; signup is the only friction. |
| Hetzner CX22 | ~€3.8/mo | Fast, no fuss. |
| Vultr / DigitalOcean / Linode | ~$5/mo | Same idea. |

> Serverless (Lambda, Cloud Run, Functions) **won't work** — they can't hold the
> long-lived UDP mappings hole-punching and relay depend on. Use a small always-on VM.

## Install (Ubuntu/Debian VM)

```bash
sudo mkdir -p /opt/lazer-rendezvous
sudo cp rendezvous_server.py /opt/lazer-rendezvous/
sudo cp rendezvous.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now rendezvous
systemctl status rendezvous          # should be active (running)
```

## Open the UDP port

**On the VM's own firewall:**
```bash
sudo ufw allow 50510/udp             # if ufw is in use
```

**In the cloud console (this is the one people forget):** add an ingress /
security-list rule allowing **UDP 50510 from 0.0.0.0/0**.
- Oracle: VCN → Security List → Ingress Rules → add *Stateless* UDP 50510.
- AWS/GCP/Azure/etc.: the instance's security group / firewall rule, UDP 50510.

## Verify

```bash
# From your laptop, expect a `SELF <your_ip> <port>` line back:
printf 'REG H AAAAAAAAAAAAAAAAAAAAAA' | nc -u -w1 <PUBLIC_IP> 50510
```
(The room above is a dummy 22-char base64url just to exercise the echo.)

## Point LazeR at it

On the **laptop**, run the server once with the host so it's remembered and baked
into the QR:

```powershell
# Windows
LazeR.exe --rendezvous <PUBLIC_IP>:50510
# or from source
python remote_server.py --rendezvous <PUBLIC_IP>:50510
```

The QR now carries `&r=<PUBLIC_IP>:50510`; a phone that **scans it** learns both
the key and the rendezvous host, and will automatically fall back to it when the
laptop isn't reachable on the local network. To turn remote off later:
`--rendezvous off`.

## Notes

- **A domain is nicer than a bare IP** (VM IPs can change). Point an A record at it
  and pass `--rendezvous rdv.example.com:50510`.
- **Load:** control traffic is negligible; direct (punched) sessions cost the rdv
  nothing after matchmaking. Only relayed sessions consume bandwidth (≈ the phone's
  packet rate, tiny). One small VM comfortably serves many users.
- **Trust:** the rdv is untrusted by design. The worst a hostile rdv can do is
  learn your public IP or refuse to connect you (DoS). It cannot forge control
  (AES-GCM) or read your traffic.
