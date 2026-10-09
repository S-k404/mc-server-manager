# Docker: the VPN (port forwarding) stack and the Docker server runtime

`mc` stays the manager. Docker only changes *where things run*; `servers/<name>/` is untouched.

## Which should I use? (this Mac)
- **VPN: Docker.** ProtonVPN in a container carries only the forwarded game port, so the Mac is not on the VPN and
  LAN play keeps working. This is the fix for "ProtonVPN blocks LAN".
- **Server: native by default.** On macOS, Docker runs in a VM (memory-capped, slower bind-mount I/O) and Docker Desktop
  becomes one more thing that must be running. Use `runtime: docker` when you want isolation/resource limits, or on a
  Linux host where Docker has no VM.

## Internet play: ProtonVPN port forwarding (`mc vpn`)
Friends join `<VPN exit IP>:<forwarded port>`. Requires a **paid Proton plan**.
Containers: `minecraft-vpn` (gluetun → ProtonVPN WireGuard, `VPN_PORT_FORWARDING=on`, `PORT_FORWARD_ONLY=on`; gluetun
redirects the forwarded port to the chosen server's port via `VPN_PORT_FORWARDING_LISTENING_PORT`) and `minecraft-forward`
(socat inside the VPN's network: server port → the same port on the host). Both on the `minecraftvps` Docker network,
`--restart unless-stopped`. Proton forwards **one** port, so one server is public: `mc vpn forward <name>`.

1. Proton account → VPN → WireGuard configuration → **tick "NAT-PMP (Port Forwarding)"** → create → copy the `PrivateKey`.
   A key made without that option connects fine but never gets a forwarded port (gluetun logs
   `port forwarding … getting external IPv4 address … read` timeouts; verified with the first key, which lacked it).
2. `mc vpn setup` (asks for the key and an exit country, e.g. Netherlands; the country needs P2P servers; with none chosen Proton picks one,
   which can be far from your friends). Writes `tunnel/vpn.env` with mode 600
   (atomically) and turns the Docker VPN on. **Exit city:** `mc vpn setup --city Amsterdam` passes gluetun's `SERVER_CITIES`; a nearer city
   means less lag. With a key already saved, `--country` / `--city` alone change only the place (no key prompt; `--city any` clears it),
   and the dashboard's Network tab has an "Exit city..." button. It applies the next `mc vpn start`. Names are checked before they reach
   the env file (letters, spaces and `. ' - ,` only). A name gluetun doesn't know for Proton stops the VPN from starting
   (`mc vpn logs vpn` says so); not checked live which city names Proton offers.
3. `mc vpn start` (or `mc up`). `mc vpn` shows health, exit and the address; `mc vpn address` prints/copies it;
   `mc vpn logs [vpn|forward]`. `mc speedtest` keeps its latency to the address (`tunnel/latency.json`, only for that exact address and
   for a day), and `mc vpn`, `mc vpn address` and the Network tab show it next to the address, so two cities can be compared.
   **Where does it stop?** `mc vpn test` walks the whole way a friend's connection goes and marks each hop `[ok]`, `[x]` or `[!]` (could not be
   checked): Docker, the saved key, the VPN container, the tunnel carrying traffic (exit address), Proton's forwarded port, gluetun's
   redirect to the server port (`iptables -t nat -S` inside the VPN), the forwarder container, something listening on the server port
   there (`/proc/net/tcp`), the server running, the forwarder reaching the server with a real status request, and a status request
   from this Mac to the public address. It prints the first failing hop and changes nothing. If this Mac's own VPN is on, the last
   hop goes Proton -> Proton, so a failure there alone proves nothing: have someone on another network try.
   **Managed network or firewall:** if it blocks the container's plain WireGuard (UDP), the stack only connects while this Mac's own
   Proton VPN (its Stealth protocol passes) is on; the container's tunnel then runs inside it. gluetun cannot speak Proton's Stealth
   protocol. The nearest thing it offers is OpenVPN over TCP (`VPN_TYPE=openvpn`, `OPENVPN_PROTOCOL=tcp`, `OPENVPN_ENDPOINT_PORT=443`,
   username with `+pmp` appended for port forwarding, from Proton's OpenVPN credentials). Not built; whether a given firewall lets it through is unknown.
   **Packet size (MTU) when this Mac's own VPN is on:** the container's tunnel then runs inside the Mac's. gluetun's default WireGuard MTU is 1400, which
   does not fit through the outer tunnel: small packets (Proton's port request, a status ping) pass while big ones (a TLS handshake, a login) are dropped, so the
   address looks fine but nobody can really join, and gluetun may report "unhealthy". `mc vpn start` therefore passes `WIREGUARD_MTU=1280` while the Mac's VPN is on
   (gluetun's default otherwise). `mc vpn mtu` shows it; `mc vpn mtu 1200` (1000-1420) or `mc vpn mtu auto` change it; `mc vpn test` prints the tunnel's real
   MTU. Not verified live.
4. The address (exit IP + port) **changes whenever the VPN reconnects**. It is re-read on every dashboard refresh, cached in
   `tunnel/public.json` for 90 s (used by "Copy invite"), shown in the status bar, Network tab, `mc doctor` and `mc share`.
5. The agent calls `vpn.heal()` every 30 s: it brings the stack back after a Docker/Mac restart and restarts the forwarder if
   the VPN was recreated. `mc vpn stop` / `mc down` turn it off on purpose and also remove a leftover `minecraft-tunnel`
   container from an older version if one is still around.

## Server in Docker (`mc runtime`)
`mc -s NAME runtime docker` (server must be stopped). Same folder, bind-mounted at `/data`; Java comes from
`eclipse-temurin:<java_major>-jre`. Container `minecraftvps-<name>`.
- RCON is published on `127.0.0.1` only; the game port on all interfaces.
- Readiness = a container healthcheck (Docker publishes the port before the server is ready).
- `keep_alive` → Docker `on-failure:3`; a clean `/stop` stays stopped. The agent handles reboot recovery and in-game stop.
- Memory limit = heap + 1.5 GB; `mc start` refuses if Docker Desktop's VM has less (Settings → Resources → Memory).
- `mc runtime native` switches back; same world either way. `mc deploy` switches the remote copy to native.
- Console: `mc console` attaches (Ctrl-c leaves); commands normally go over RCON, falling back to the container's stdin.

## The fake VPS (`minecraftvps`)
An Ubuntu 24.04 systemd container used to live-test `mc remote`/`mc deploy` (SSH on `localhost:2222`, game port
`25565 → 25570`). Remote name `minecraftvps`. `loginctl enable-linger mcuser` is required, or the agent and tmux
die when the last SSH session closes.

## Sign-in proxy: Microsoft sign-in without a Mac-wide VPN (`mc vpn proxy`, `mc net auth`)
Some managed Wi-Fi networks block Minecraft/Microsoft sign-in. The usual workaround, a Mac-wide VPN, then blocks friends on
the same Wi-Fi. Instead the same `minecraft-vpn` container runs gluetun's HTTP proxy (`HTTPPROXY=on`, `HTTPPROXY_STEALTH=on`),
published on this Mac only (`-p 127.0.0.1:8888:8888`), with a random password in `tunnel/proxy.env` (mode 600, handed over as a
Docker env file, never on a command line; `mc support` hides it). gluetun's firewall accepts the published port by itself (its
source allows packets arriving on the container's local-network interface "to reach Gluetun").

- `mc vpn start` creates the password the first time and starts the proxy. A VPN started before this existed has no proxy until
  the next `mc vpn start`. It works with any Proton plan (only port forwarding needs the paid plan and the NAT-PMP key).
- `mc vpn proxy [--copy]` prints what to enter in **Prism Launcher: Settings -> Proxy -> HTTP** (address, port, user, password).
  The dashboard's Network tab has the same under "Sign-in proxy". It is an HTTP proxy on purpose: a SOCKS setting in the JVM
  would also capture the game's own connection to servers.
- `mc net auth` (also part of `mc doctor`) connects to the eight sign-in hosts (`login.live.com`, `login.microsoftonline.com`,
  `user.auth.xboxlive.com`, `xsts.auth.xboxlive.com`, `api.minecraftservices.com`, `sessionserver.mojang.com`,
  `textures.minecraft.net`, `piston-meta.mojang.com`), in parallel, straight from this Mac and through the proxy, and says which
  are blocked. With a Mac-wide VPN on it skips the direct test (it would measure the VPN, not the Wi-Fi).

  | Host | What needs it | If it is blocked |
  |---|---|---|
  | `login.live.com`, `login.microsoftonline.com` | the launcher's Microsoft sign-in | use the proxy (`mc vpn proxy`) |
  | `user.auth.xboxlive.com`, `xsts.auth.xboxlive.com` | Xbox Live / XSTS tokens for the account | use the proxy |
  | `api.minecraftservices.com` | Minecraft profile and entitlement check | use the proxy |
  | `sessionserver.mojang.com` | the game joining a server, **and an online-mode server verifying every player** | see the next point |
  | `textures.minecraft.net` | skins and capes | cosmetic only; no fix is offered |
  | `piston-meta.mojang.com` | the version list (the first thing `mc create` and `mc upgrade` read) | `mc` cannot see which versions exist |

  The verdict line ("Sign-in") looks only at the first five rows: all reachable straight away means no VPN or proxy is needed;
  blocked straight away but reachable through the proxy means "put the proxy in your launcher"; blocked both ways points at `mc vpn logs`.
- **Online-mode servers:** the Minecraft server itself checks every player with `sessionserver.mojang.com` from this Mac, and the
  vanilla server ignores proxy settings for that. If it is blocked directly, `mc net auth` says so: either keep the Mac VPN on with
  Proton's "Allow LAN connections", or run that server in offline mode with logins (`mc cracked on`).
- **Not verified live** (needs the Mac with Docker, a Proton key and Prism): that Prism passes the proxy to the game's own
  authentication calls and not only to its own sign-in screen; that `docker run -p` on Docker Desktop reaches gluetun's proxy.
- **Simple Voice Chat through the internet address** (`mcctl/voice.py`, built 2026-10-07, NOT verified live). Proton forwards one port, for TCP and UDP alike.
  The forwarder also listens for UDP on that port (`socat UDP4-LISTEN:<port> -> UDP4:<host gateway>:<the mod's port>`) and `voice_host=<exit ip>:<port>` in the mod's
  `voicechat-server.properties` tells every player to send audio there. Because `voice_host` is global it is only set while everyone arrives through the address
  anyway: **hidden mode**, or while **this Mac's own VPN is on** (it blocks the Wi-Fi); with the Mac's VPN off in local / default mode audio goes straight to this
  Mac and a `voice_host` that mc wrote is taken out again. One you wrote yourself is never touched. The address changes at every VPN reconnect and the mod reads its
  config only at start, so the agent rewrites the line and restarts the server (15 s warning) once the change has stood for two minutes and nobody is on; starting a
  server writes it first. `mc vpn voice` shows and repairs it; `mc vpn test` has five voice hops. Other UDP-only mods are still not forwarded. Players need the mod too.

## Mod notes: a tiny local model that reads a mod's page (`mc llm`, `mc mods explain`)
Off until `mc llm start`. That runs one container, `minecraft-llm` (the official `ollama/ollama` image), published on
`127.0.0.1:11434` only, with its models in the Docker volume `minecraft-llm-models`, `--cap-drop ALL`, a 4 GB memory cap and
`--restart unless-stopped`. The first start downloads about 1 GB (the model `qwen2.5:1.5b`; change it with `mc llm model <name>`,
see ollama.com/library). Ollama checks every layer by sha256 and unloads the model by itself a few minutes after the last
question, so it costs almost nothing while idle. Docker Desktop on a Mac has no GPU access, so it runs on the CPU.

- After `mc mods add <slug>` (or the Mods tab's install button) the page of **each mod you asked for** is read, not its
  dependencies and not the starter mods of a new server, and a note is printed: one sentence on what it does, and what you must do
  after installing, with the sentence from the page that says so. `mc mods explain <name>` (and the Mods tab's "Explain" button)
  does the same for any Modrinth slug, installed mod or hand-added jar (it uses the jar's own `description`). `mc llm notes off`
  stops the automatic notes; answers are cached in `.mc-modnotes.json` per page and model.
- **A tiny model makes things up, so its answers are checked in code:** a setup step is shown only if the model's quote is really
  on the page, a port only if that number is on the page, and whether players must install the mod too is never asked of the
  model (Modrinth states it). Mod pages are untrusted text; the answer is only displayed, never run.
- **It can only say what the page says.** Simple Voice Chat's own README says just "this mod requires special setup on the server,
  read the wiki", so the model can flag that but cannot name the port. For that mod `mc mods doctor` has a separate deterministic
  check (its config file, UDP port, `bind_address` vs `server-ip`, whether anything listens on the port, Docker runtime, the
  Proton path), written from the mod's source.
- **Not verified live** (needs Docker and the model registry): a real model's answers, the default model name, and the speed on
  your Mac. The tests use a local HTTP server that imitates Ollama's documented `/api/chat`, `/api/tags` and `/api/pull`.
- Docker Desktop's built-in model runner (`docker model`) is a possible alternative that can use the Mac's GPU; it was not used
  because it is a newer, optional Docker feature and a plain container works on any Docker, including the Linux VPS.

## Share modes: hidden, or a local name (`mc share mode hidden|local`)
Until you choose, nothing changes: the invite shows the Wi-Fi address. The choice is machine-wide (the `share_mode` setting) and applies
when a server next starts (`mc` says which running servers need a restart; `mc -s <name> restart --in 2m` warns players first).

- **hidden**: a native server gets `server-ip=127.0.0.1` at every start (a Docker server publishes its game port on `127.0.0.1`), so nothing on
  the Wi-Fi can connect. Friends join only through the Docker VPN's forwarded address; the invite shows only that, and says "not ready yet"
  when it doesn't exist (it never falls back to the Wi-Fi address). Costs about 60-120 ms; the address changes when the VPN reconnects (the
  alert posts the whole new invite); every player appears from the forwarder's address, so IP bans don't apply; voice chat travels through the
  same address (see above). Native RCON becomes loopback-only too. A `server-ip` you set yourself to another address is
  replaced by hidden mode and left alone by local mode.
- **local**: the server listens on the Wi-Fi as before, but the invite carries a name, `mc-ab12.local`, published with macOS's own
  `dns-sd -P` (a child process the agent keeps alive and re-publishes when the Wi-Fi address changes; it changes no system setting). This hides
  the number from casual view only: anyone on the same Wi-Fi who looks can still find this Mac, and the Mac's own Bonjour name (`scutil --get
  LocalHostName`, read only, e.g. `Jordans-MacBook-Pro`) is visible whatever mc does; `mc doctor` warns when it looks personal.
- **One place decides what may be shown**: `network.display_address(server)`. The invite, Overview, Network tab, `mc info`, the speed test, the
  doctor and the alert text all use it; `support.redact` masks addresses in the diagnostics bundle; `mc share --show-ip` reveals the Wi-Fi address
  on purpose. A dashboard test walks every tab in hidden mode and fails if the Wi-Fi address appears anywhere.
- **Choosing it:** the first time `mc` opens its dashboard with no mode chosen (and a server to share) it asks once: Hidden, Local name or
  Not now. "Hidden" with no VPN key goes straight on to the key dialog. It never asks again (`share_wizard_done`); the Network tab's
  "Play together" card keeps the two buttons and a live checklist, and `mc share mode` does the same from the terminal. The status bar
  on every tab shows the mode, the address that mode allows (hidden with no VPN says "nobody can join"), `proxy up/down`, and
  `Mac VPN on: LAN blocked` where Wi-Fi friends exist (not in hidden mode).
- **Hidden mode is checked, not assumed.** It needs Docker Desktop to deliver the forwarder's connection (to the host gateway) to this Mac's loopback.
  `mc doctor` sends a real Minecraft status request through the forwarder container and reports whether the server answered; if it doesn't, it
  suggests `mc share mode local`. Never verified on a real Mac yet.
- `mc doctor` in hidden mode wants the opposite of before: the server must NOT be reachable on the Wi-Fi address.

## LAN play while a Mac-wide VPN is on
With ProtonVPN's own app on, friends on the Wi-Fi can't reach this Mac unless Proton's **Allow LAN connections** is on
(`mc doctor` reads that setting, read-only, and says so). Turning it on only exempts local-network traffic from the tunnel;
internet traffic stays on the VPN. `mc` never edits Proton's settings, routes or the firewall. The cleaner option is the
Docker VPN above: the Mac isn't on a VPN at all, so LAN play always works.

## Security notes
- Every container runs with `--cap-drop ALL --security-opt no-new-privileges` (gluetun keeps only `NET_ADMIN`); servers also have a PID and memory limit.
- RCON is published on `127.0.0.1` only (`mc doctor` checks it); the game port is the only thing exposed (and with hidden mode, not even that: it is on `127.0.0.1` too).
- gluetun's own firewall is the kill switch: tunnel traffic can't leave outside the VPN. Its only opening is the host-gateway /32
  so the forwarder can reach the server port.
- `tunnel/vpn.env` is mode 600 and written atomically; the key is read from the env file, never put on a command line.
- `tunnel/proxy.env` (the sign-in proxy password) is mode 600 too; the proxy is published on `127.0.0.1` only.
- `mc cracked on` installs EasyAuth first and refuses on a vanilla server (override: `--insecure`).
