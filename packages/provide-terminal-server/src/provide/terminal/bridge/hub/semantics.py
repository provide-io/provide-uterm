import shlex


class CommandSplitter:
    """
    Splits shell command chains into individual commands while respecting quotes and escapes.
    Supports: ;, &&, ||, |
    """

    def split(self, command: str) -> list[str]:
        if not command:
            return []

        lexer = shlex.shlex(command, posix=True)
        lexer.whitespace_split = False
        # We want to keep most characters together, but split on our target operators.
        # shlex by default splits on whitespace. We want to find the operators.

        # A simpler approach might be to use shlex to find the positions of the operators
        # that are NOT inside quotes.

        parts = []

        # Operators to split by

        # We use shlex to tokenize the input, but we need it to NOT split on everything.
        # Actually, let's use a more manual approach with shlex.split's logic or similar.

        # Let's try to use shlex.shlex but configure it.
        # Or better, just iterate and track quote state.

        in_single_quote = False
        in_double_quote = False
        escaped = False

        i = 0
        start = 0
        while i < len(command):
            char = command[i]

            if escaped:
                escaped = False
                i += 1
                continue

            if char == "\\" and not in_single_quote:
                escaped = True
                i += 1
                continue

            if char == "'" and not in_double_quote:
                in_single_quote = not in_single_quote
                i += 1
                continue

            if char == '"' and not in_single_quote:
                in_double_quote = not in_double_quote
                i += 1
                continue

            if not in_single_quote and not in_double_quote:
                # Check for operators
                found_op = None
                op_len = 0

                if command.startswith("&&", i):
                    found_op = "&&"
                    op_len = 2
                elif command.startswith("||", i):
                    found_op = "||"
                    op_len = 2
                elif command[i] == ";":
                    found_op = ";"
                    op_len = 1
                elif command[i] == "|":
                    found_op = "|"
                    op_len = 1

                if found_op:
                    part = command[start:i].strip()
                    if part:
                        parts.append(part)
                    start = i + op_len
                    i += op_len
                    continue

            i += 1

        final_part = command[start:].strip()
        if final_part:
            parts.append(final_part)

        return parts
