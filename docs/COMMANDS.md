# Every `mc` command

Generated from the CLI itself. Add `-s NAME` (or `-s all`) to act on a server; `mc -r REMOTE ...` runs on a VPS;
`mc COMMAND -h` shows every option. Run `mc` alone for the live dashboard (Ctrl+P = command palette).

In the dashboard's Live tab: plain text is a server command; `/start`, `/game time night`, `/backup`... run these `mc` commands.

```
mc agent                             Background agent: autostart, crash restarts, schedules, notifications.
mc agent install                     Start the agent now and at every login.
mc agent logs                        Show what the agent has been doing.
mc agent notify <what> <state>       Mac notifications for crashes or player joins.
mc agent uninstall                   Remove the agent.
mc alerts                            Alerts (lag, RAM, disk, internet address) by Mac notification and optional Discord webhook.
mc alerts joins <state>              Also post joins/leaves to the Discord webhook.
mc alerts off
mc alerts on
mc alerts test                       Send a test alert through every channel.
mc alerts webhook <url>              Send alerts to a Discord channel (the URL is a secret: stored owner-only, never shown).
mc backup                            World backups.
mc backup auto <state>               Back up automatically on every stop.
mc backup list                       List backups (newest first).
mc backup prune [keep]               Delete old zip backups, keeping the newest N.
mc backup push [remote_name]         Copy the newest verified backup to a remote machine (nothing is deleted there).
mc backup restore <which>            Restore a backup (current world is kept in backups/).
mc backup verify [which]             Check backups are readable zips containing a world.
mc ban <name> [reason]               Ban a player.
mc bans                              Banned players with their reasons.
mc cancel                            Cancel a restart/stop countdown (mc restart --in 5m).
mc cat [setting]                     Show a cat. `mc cat off` / `mc cat on` turns the occasional cats after commands off/on.
mc clone <source> <new>              Copy a server's mods and settings into a new server.
mc cmd <command>                     Run any server console command and show the reply.
mc console                           Live server console (type commands; Ctrl-b then d to leave it running).
mc cracked [state]                   Let non-Mojang ("cracked") accounts join: on (adds EasyAuth logins) / off. No argument shows the state.
mc crash                             Explain why the server last crashed.
mc create [name]                     Create a new server (asks questions if you leave things out).
mc dash                              Open the interactive dashboard.
mc datapack                          Datapacks: list, add, remove.
mc datapack add <path>               Add a datapack to the world (a running server runs /reload).
mc datapack install <names>          Install datapacks from Modrinth (a running server runs /reload).
mc datapack remove <name>            Move a datapack out of the world into backups/.
mc datapack search <query>           Find datapacks on Modrinth that work on this server's version.
mc datapack update                   Update Modrinth datapacks that have a newer build.
mc delete <name>                     Move a server into backups/ (never erased).
mc deop <names>                      Remove operator.
mc deploy <remote_name>              Copy a stopped server to a remote with rsync (never deletes anything remotely).
mc doctor                            Check for lag, VPN, firewall, port and security problems.
mc down                              Stop server(s); stops the Docker VPN when nothing else is running.
mc events                            What happened recently: starts, stops, backups, crashes, joins, alerts.
mc fix                               Repair everything mc can, in order: Docker, servers, share mode, the internet path, voice chat. Safe to run any time.
mc fly [state]                       Allow or forbid flying (server allow-flight). No argument shows the state.
mc game                              In-game helpers: time, weather, modes, teleports, items, border, who's where, stats, feed.
mc game border [size]                Show the world border, or set its size.
mc game feed [lines]                 Recent joins, leaves, chat, advancements and deaths.
mc game give <name> <thing> [count]  Give a player items: mc game give Steve diamond 16
mc game info                         World summary: version, seed, day, border, difficulty, who's online.
mc game mode <mode> <name>           Set a player's game mode: mc game mode creative Steve
mc game save                         Flush the world to disk right now (no backup).
mc game seed                         Show the world seed.
mc game spawn <x> <y> <z>            Set the world spawn point.
mc game time <value>                 Set the time of day.
mc game top [stat]                   Leaderboard from the world's stats (works while the server is stopped).
mc game tp <who> <target>            Teleport a player to another player or to coordinates.
mc game weather <kind> [minutes]     Change the weather.
mc game where [name]                 Where players are (coordinates + dimension).
mc info                              Addresses players use to join (what your share mode allows: see mc share mode).
mc kick <name> [reason]              Kick a player.
mc kill                              Force-stop a frozen server (loses progress since the last autosave).
mc llm                               A tiny local language model, in Docker, that reads a new mod's page and tells you what setup it needs. Off until `mc llm start`.
mc llm model [name]                  Show or change the model (see ollama.com/library; a small one is plenty).
mc llm notes <state>                 Turn the notes after each new mod install on or off (the model stays available for `mc mods explain`).
mc llm start                         Start the model container (first time: downloads its image and the model, about 1 GB).
mc llm stop                          Stop it. Downloaded models stay in the Docker volume.
mc loaders                           What you can run: Fabric, Quilt, NeoForge, Forge, Paper or vanilla, and what each is for.
mc logs [lines]                      Show the server log.
mc mods                              Mods from Modrinth (filtered to the server's version + loader).
mc mods add <names>                  Install mods (+ required dependencies).
mc mods add-jar <path>               Copy a downloaded Fabric mod .jar into mods/ (for mods that aren't on Modrinth).
mc mods config                       Look at every mod's config file: unreadable ones, empty ones, and Simple Voice Chat's port. --fix repairs them.
mc mods doctor [jar]                 Find mods that need something (a library, setup, another version) or clash, and say how to fix it.
mc mods explain <name>               Have the tiny local model read a mod's page and say what it does and what setup it needs (needs: mc llm start).
mc mods pin <name>                   Keep a mod at its current version.
mc mods remove <names>               Uninstall mods.
mc mods search <query>               Find mods that work on this server.
mc mods unpin <name>                 Allow a pinned mod to update again.
mc mods update                       Update all unpinned mods.
mc net                               Network checks.
mc net auth                          Which Minecraft/Microsoft sign-in servers this network lets through, straight and via the Docker VPN proxy.
mc op <names>                        Make players operators.
mc pack                              Modpacks for friends' game clients.
mc pack clear                        Stop offering a resource pack.
mc pack export [folder]              Make a .mrpack of the client mods players need (import it in Modrinth App / Prism).
mc pack set <url>                    Offer a resource pack to players when they join (downloads it once to compute the checksum).
mc password                          EasyAuth login passwords.
mc password reset <name>             Forgotten password: lets the player /register again.
mc password set <name> <password>    Set a player's login password.
mc player <name>                     Everything known about one player: roles, ban, playtime, deaths, advancements, last seen.
mc players                           Everyone the server knows: online, ops, whitelist, bans.
mc ram [gb]                          Show or change the server's max RAM.
mc remote                            Remote machines (a VPS) you run mc on over SSH: mc -r vps status.
mc remote add <name> <target>        Save a remote.
mc remote list                       Saved remotes.
mc remote remove <name>              Forget a remote (touches nothing on it).
mc remote setup <name>               Install mc itself on the remote (copies the tool only, no worlds).
mc rename <new>                      Rename the selected server (its folder, backups and settings move with it). Stop it first.
mc res                               System + server resource usage (CPU, RAM, disk, network, lag).
mc restart [which]                   Stop then start: mc restart, mc restart all.
mc runtime [where]                   Show or change where a server runs: natively (tmux) or in a Docker container. Same folder either way.
mc say <message>                     Broadcast a chat message.
mc schedule [kind] [value]           Automatic backups / daily restarts: mc schedule backup 6h | mc schedule restart 05:00.
mc servers                           List all servers with version, port, RAM, state and players.
mc set [key] [value]                 Show all settings, or change one: mc set difficulty hard.
mc share                             Invite message for friends, and how they find your server (hidden, or by a local name).
mc share mode [choice]               Show or choose how friends find your server: hidden (only the Docker VPN address) or local (a name, not the number).
mc speedtest                         Latency to the server, to each address people join by, and to each connected player.
mc start [which]                     Start the server (several can run at once): mc start, mc start all.
mc status                            State, players, mods and settings at a glance.
mc stop [which]                      Stop and save: mc stop, mc stop all. A server that won't stop doesn't keep the others running.
mc support                           Everything useful for a bug report in one block (copied to the clipboard; passwords and keys are stripped).
mc tell <name> <message>             Send a private message to one online player.
mc unban <name>                      Unban a player.
mc up                                Start server(s) + the Docker VPN (internet play) when it's set up.
mc upgrade [version]                 Move a server (and its mods) to another Minecraft version, safely.
mc use <name>                        Pick the default server.
mc versions                          Minecraft versions you can create a server on (newest first), with the loaders that support each.
mc vpn                               Internet play: ProtonVPN port forwarding in Docker (paid Proton plan). Friends join the VPN's IP and forwarded port; the Mac stays off the VPN so LAN play keeps working.
mc vpn address                       Show (and copy) the address friends use to join from the internet.
mc vpn fix                           Check the internet path and repair it: recreates the VPN when the tunnel, port or forwarder is bad (the address changes), then checks again.
mc vpn forward [name]                Choose which server the internet address leads to (Proton forwards one port, so one server is public).
mc vpn logs [which]                  Show container logs (the forwarded port shows up in the vpn logs).
mc vpn mtu [value]                   Show or set the container tunnel's packet size. A smaller one helps when this Mac's own VPN is on (a tunnel inside a tunnel).
mc vpn proxy                         Settings for your launcher's sign-in proxy (so Microsoft sign-in works without a Mac-wide VPN).
mc vpn setup                         Save your ProtonVPN WireGuard key (made with NAT-PMP / Port Forwarding ticked) and turn the Docker VPN on.
mc vpn start
mc vpn status                        Show the VPN, the forwarder, the internet address and what is wrong (same as plain `mc vpn`).
mc vpn stop
mc vpn test                          Check every hop of the internet path (VPN, forwarded port, redirect, forwarder, server) and show where it stops.
mc vpn voice                         Simple Voice Chat through the internet address: where audio goes and the address players are given. Rewrites it when it is stale.
mc whitelist                         Whitelist: list, add, remove, on, off.
mc whitelist add <names>
mc whitelist off
mc whitelist on
mc whitelist online
mc whitelist remove <names>
mc world                             Import, export or rename the world.
mc world export [folder]             Zip the world to share it.
mc world import <path>               Swap in a world from a .zip or folder (old one kept).
mc world rename <new>                Rename the world (its folder and level-name). Stop the server first.
```
