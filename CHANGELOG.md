# Changelog

All notable changes to this project are written here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

The first public version: a command line and a dashboard for running Minecraft servers on your own machine.

### Added
- **Servers.** Create, start, stop, restart (with countdown warnings), clone, rename, delete (into `backups/`), upgrade to another
  Minecraft version, and run natively in tmux or in a Docker container on the same folder. Loaders: Fabric, Quilt, NeoForge, Forge,
  Paper and vanilla. Java runtimes are downloaded per Minecraft version.
- **A dashboard** (`mc` alone): Live console with a command line, Overview, Players, Game, Files (editor, import and export), Mods and
  datapacks, Backups, Network, Settings and Resources, with a status bar on every tab.
- **A background agent** (launchd on macOS, systemd on Linux): autostart, crash restarts, scheduled backups and restarts,
  notifications, alerts to a Discord webhook, and care of the VPN stack.
- **Mods.** Modrinth search, install with dependencies, update and pin; `mc mods doctor` finds what is missing, in conflict or built for
  the wrong version, and fixes the safe cases; `mc mods config` checks every mod's config file; optional notes on a new mod from a tiny
  local model; `.mrpack` import and export.
- **Players.** Operators, whitelist, bans, kicks, messages, per-player profiles, offline-mode logins with EasyAuth, and UUID migration
  when switching modes. `mc game` helpers for time, weather, teleports, items and the border.
- **Backups** that are verified, with retention, offsite copies and a restore that keeps the current world.
- **Internet play.** A ProtonVPN port forward in Docker (`mc vpn`), a sign-in proxy for launchers, share modes (`hidden`, `local`),
  Simple Voice Chat through the forwarded address, `mc net auth`, `mc vpn test` (shows which hop of the path fails), `mc fix`
  (repairs what it can, in order) and `mc speedtest`. `mc remote` and `mc deploy` run servers on a VPS.
- **Diagnostics.** `mc doctor`, `mc support` (redacted), an event log and resource history.
- **Safety.** Atomic writes, per-server operation locks, disk-space checks, an error log, validated console input, owner-only secret
  files, and a rule that nothing is ever deleted: worlds and servers are moved into `backups/`.
- **Project files.** README, LICENSE (MIT), CONTRIBUTING, CODE_OF_CONDUCT, SECURITY, issue and pull-request templates, continuous
  integration on Ubuntu and macOS, Dependabot, ruff, and `scripts/make_screenshots.py` for the pictures in `docs/images/`.
