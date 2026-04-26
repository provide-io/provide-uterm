from provide.terminal.bridge.hub.semantics import CommandSplitter


def test_split_simple_command():
    splitter = CommandSplitter()
    assert splitter.split("ls") == ["ls"]


def test_split_semicolon():
    splitter = CommandSplitter()
    assert splitter.split("ls; rm -rf /") == ["ls", "rm -rf /"]


def test_split_logical_and():
    splitter = CommandSplitter()
    assert splitter.split("cmd1 && cmd2") == ["cmd1", "cmd2"]


def test_split_logical_or():
    splitter = CommandSplitter()
    assert splitter.split("cmd1 || cmd2") == ["cmd1", "cmd2"]


def test_split_pipe():
    splitter = CommandSplitter()
    assert splitter.split("cmd1 | cmd2") == ["cmd1", "cmd2"]


def test_split_complex_chain():
    splitter = CommandSplitter()
    assert splitter.split("cmd1 && cmd2 || cmd3") == ["cmd1", "cmd2", "cmd3"]


def test_split_with_quotes():
    splitter = CommandSplitter()
    assert splitter.split('echo "hello; world"') == ['echo "hello; world"']
    assert splitter.split("echo 'hello; world'") == ["echo 'hello; world'"]


def test_split_with_escapes():
    splitter = CommandSplitter()
    assert splitter.split("echo hello\\; world") == ["echo hello\\; world"]
