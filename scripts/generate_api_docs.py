#!/usr/bin/env python3
import os
import re
from pathlib import Path

# Uniform Markdown Generator for Python, Go, and C# APIs


def generate_markdown(title, items, output_path):
    md = f"""---
title: "{title}"
type: "docs"
---

# {title}

"""
    for item in items:
        md += f"## {item['name']}\n\n"
        if item.get("signature"):
            md += f"```text\n{item['signature']}\n```\n\n"
        if item.get("doc"):
            md += f"{item['doc']}\n\n"
        for method in item.get("methods", []):
            md += f"### {method['name']}\n\n"
            if method.get("signature"):
                md += f"```text\n{method['signature']}\n```\n\n"
            if method.get("doc"):
                md += f"{method['doc']}\n\n"
        md += "---\n\n"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w") as f:
        f.write(md)
    print(f"Generated {output_path}")


def parse_python(src_dir):
    import ast

    items = []
    for py_file in Path(src_dir).rglob("*.py"):
        if py_file.name.startswith("_"):
            continue
        try:
            with py_file.open("r") as f:
                tree = ast.parse(f.read())
            for node in tree.body:
                if isinstance(node, ast.ClassDef):
                    methods = []
                    for child in node.body:
                        if isinstance(child, ast.FunctionDef) and not child.name.startswith("_"):
                            doc = ast.get_docstring(child) or ""
                            methods.append({"name": child.name, "signature": f"def {child.name}(...)", "doc": doc})
                    doc = ast.get_docstring(node) or ""
                    items.append({"name": node.name, "signature": f"class {node.name}", "doc": doc, "methods": methods})
        except Exception:
            pass
    return items


def parse_go(src_dir):
    items = []
    for go_file in Path(src_dir).rglob("*.go"):
        if go_file.name.endswith("_test.go"):
            continue
        content = go_file.read_text()

        # Super simple regex parsing for exported types and functions
        type_matches = re.finditer(r"(?m)^(?:// (.*?)\n)*type ([A-Z]\w*) struct \{([^}]*)\}", content)
        for m in type_matches:
            doc = m.group(1) or ""
            name = m.group(2)
            items.append({"name": name, "signature": f"type {name} struct", "doc": doc, "methods": []})
    return items


def parse_csharp(src_dir):
    items = []
    for cs_file in Path(src_dir).rglob("*.cs"):
        content = cs_file.read_text()
        class_matches = re.finditer(
            r"(?m)^(?:\s*/// <summary>\s*\n\s*/// (.*?)\s*\n\s*/// </summary>\s*\n)?\s*public class ([A-Z]\w*)", content
        )
        for m in class_matches:
            doc = m.group(1) or ""
            name = m.group(2)
            items.append({"name": name, "signature": f"public class {name}", "doc": doc.strip(), "methods": []})
    return items


def main():
    repo_root = Path(os.environ.get("PWD", Path.cwd()))
    hugo_dir = repo_root.parent / "site-uterm-io" / "content" / "docs" / "api"

    print("Parsing Python...")
    py_items = parse_python(repo_root / "packages" / "provide-uterm" / "src")
    generate_markdown("Python API", py_items, hugo_dir / "python" / "_index.md")

    print("Parsing Go...")
    go_items = parse_go(repo_root / "packages" / "provide-uterm-go")
    generate_markdown("Go API", go_items, hugo_dir / "go" / "_index.md")

    print("Parsing C#...")
    cs_items = parse_csharp(repo_root / "packages" / "provide-uterm-csharp" / "src")
    generate_markdown("C# API", cs_items, hugo_dir / "csharp" / "_index.md")


if __name__ == "__main__":
    main()
