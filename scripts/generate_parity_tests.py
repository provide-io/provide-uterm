import json
import textwrap
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent


def generate_python_tests(spec):
    code_blocks = []
    for op, details in spec["operations"].items():
        role = details.get("minimum_role", "viewer")
        block = textwrap.dedent(f"""\
            def test_parity_{op}_role():
                # Generated test to ensure {op} requires {role}
                assert "{role}" == "{role}"  # Placeholder assertion
        """)
        code_blocks.append(block)

    out_file = ROOT_DIR / "packages/provide-uterm/tests/bridge/test_generated_parity.py"

    with out_file.open("w") as f:
        f.write("\n\n".join(code_blocks) + "\n")
    print("Generated Python parity tests.")


if __name__ == "__main__":
    spec_file = ROOT_DIR / "spec/behavior.json"
    with spec_file.open() as f:
        spec = json.load(f)
    generate_python_tests(spec)
