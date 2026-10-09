from mcctl import agent, cli, remote


def test_pop_remote_flag():
    assert cli.pop_remote_flag(["-r", "vps", "status"]) == ("vps", ["status"])
    assert cli.pop_remote_flag(["status", "--remote=vps"]) == ("vps", ["status"])
    assert cli.pop_remote_flag(["status"]) == (None, ["status"])


def test_remote_command_quotes_and_tty():
    r = {"host": "h", "user": "u", "path": "~/mc", "port": 2222}
    cmd = remote.remote_command(r, ["say", "hi there"], tty=True)
    assert cmd[:2] == ["ssh", "-t"] and "u@h" in cmd
    assert cmd[-1] == "cd ~/mc && ./mc say 'hi there'"


def test_systemd_unit_text():
    t = agent.unit_text()
    assert "ExecStart=" in t and "-m mcctl agent run" in t and "Restart=always" in t
