# Running mc on a VPS (Linux)

1. Make an SSH key login to the VPS (`ssh-copy-id user@host`). mc uses `BatchMode`, so password prompts fail.
2. On the VPS: `sudo apt install tmux rsync curl` (Java is downloaded by mc itself).
3. Here: `mc remote add vps user@host` then `mc remote setup vps` (copies the tool, runs `install.sh`).
4. `mc stop` the server locally, then `mc deploy vps --start` (rsync; nothing is deleted on the VPS).
5. Open the port on the VPS: `sudo ufw allow 25565/tcp` (and the cloud provider's firewall/security group).
   Do NOT expose RCON (25575): keep it closed.
6. Keep it running: `mc -r vps agent install` then `ssh user@host sudo loginctl enable-linger user`.
7. Day to day: `mc -r vps status`, `mc -r vps console`, `mc -r vps backup now`.

A VPS has a public IP, so players join at `<VPS IP>:25565`: no tunnel or VPN needed. Offline-mode + EasyAuth still applies.

`mc deploy` of a server whose runtime is `docker` switches the remote copy to `native` (the VPS runs it with its own Java).
The local test VPS is the Docker container `minecraftvps` (see `docs/DOCKER.md`).
