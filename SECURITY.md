# Security policy

## Reporting a vulnerability

Please **do not open a public issue** for a security problem.

Report it privately with GitHub's *Report a vulnerability* button on this repository's
[Security tab](https://github.com/S-k404/mc-server-manager/security/advisories/new). Include what you found, how to reproduce
it, and which version (the commit you ran) and system you saw it on. You will get an answer within a few days; this is a
volunteer project, so please be patient.

If a report contains a real secret (a VPN key, a webhook URL, a password), replace it with a placeholder first. `mc support`
already strips the usual ones from its output.

## Supported versions

Only the latest release on `main` gets security fixes.

## What is in scope

`mc` is a local tool that runs on your own machine and manages servers you own. These are the areas where a flaw would matter:

- **Command injection.** Anything typed by a person that ends up in a server console command or a shell command
  (player names, chat text, item ids, place names, file names, mod names) must be validated. A way around that is a bug.
- **Secrets.** `tunnel/vpn.env` (the ProtonVPN key), `tunnel/proxy.env` and the Discord webhook in `.mc-settings.json` are
  owner-only (mode 600) and never appear on a command line. None of those, and no server's RCON password, may appear in
  `mc support` output or in logs.
- **Downloads.** Server jars, mods, datapacks and modpack files are checked against the checksum their source publishes.
  A way to install something that does not match is a bug. (Known gap: the Java runtime download is not yet checked against
  Adoptium's published SHA-256.)
- **File access.** The dashboard's file editor must not read or write outside the server's folder (including through symlinks).
- **Exposure.** A Docker server's RCON port is published on localhost only, and in `hidden` share mode nothing listens on the
  Wi-Fi address. A way around either is a bug. (A native server's RCON listens like its game port does, protected by a random
  32-character password that `mc` only ever uses over 127.0.0.1.)
- **Data loss.** `mc` should never delete a world or a server; it moves them into `backups/`.

Out of scope: problems in Minecraft itself, in a mod or plugin you installed, in Docker, ProtonVPN or gluetun, or that need an
attacker who already controls your user account.

## Good practice when running a server

- Keep RCON (default port 25575) closed on the firewall, and run `mc doctor` now and then: it checks the RCON setup.
- Leave the whitelist on. `mc cracked on` (offline-mode accounts) installs a login mod first for a reason.
- Keep the repository folder private to your user: it holds your worlds, backups and the files above.
