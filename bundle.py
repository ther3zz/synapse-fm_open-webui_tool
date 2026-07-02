"""
bundle.py — Produces a single-file Open WebUI tool from modular source.

Open WebUI requires tools to be entirely self-contained in one Python file.
This script reads synapsefm_tool.py and inlines all module code (player_builder,
bootloader), replacing the try/except import blocks with actual function code.

Usage:
    python bundle.py
    python bundle.py --output custom_name.py

Output: dist/synapsefm_tool.py (ready to paste into Open WebUI)
"""

import argparse
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SOURCE_FILE = os.path.join(SCRIPT_DIR, "synapsefm_tool.py")
DIST_DIR = os.path.join(SCRIPT_DIR, "dist")
DEFAULT_OUTPUT = "synapsefm_tool.py"

# Module definitions: (file_path, label, start_marker, end_marker)
MODULES = [
    (
        os.path.join(SCRIPT_DIR, "modules", "player_builder.py"),
        "Player builder",
        "# --- BEGIN MODULE: player_builder ---",
        "# --- END MODULE: player_builder ---",
    ),
    (
        os.path.join(SCRIPT_DIR, "modules", "bootloader.py"),
        "Bootloader",
        "# --- BEGIN MODULE: bootloader ---",
        "# --- END MODULE: bootloader ---",
    ),
]


def read_file(path):
    """Read a file and return its contents."""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def extract_module_body(module_source):
    """
    Extract code from a module file, stripping the module-level docstring.
    Returns the code ready for inlining.
    """
    lines = module_source.split("\n")
    result_lines = []
    in_docstring = False
    docstring_done = False

    for line in lines:
        # Skip the module-level docstring
        if not docstring_done:
            stripped = line.strip()
            if stripped.startswith('\"\"\"') and not in_docstring:
                in_docstring = True
                # Check if it's a single-line docstring
                if stripped.endswith('\"\"\"') and len(stripped) > 3:
                    docstring_done = True
                continue
            if in_docstring:
                if '\"\"\"' in stripped:
                    docstring_done = True
                continue

        result_lines.append(line)

    return "\n".join(result_lines).strip()


def validate_sources():
    """Pre-bundle validation to catch corruption before it ships."""
    errors = []

    for module_path, label, _, _ in MODULES:
        if not os.path.isfile(module_path):
            errors.append(f"{label}: file not found at {module_path}")
            continue

        content = read_file(module_path)
        lines = content.split("\n")

        # Check for non-ASCII in comments (sign of encoding corruption)
        for i, line in enumerate(lines, 1):
            stripped = line.lstrip()
            # Only check comment lines and non-string Python lines
            if stripped.startswith("#") or stripped.startswith("//"):
                for ch in stripped:
                    if ord(ch) > 127:
                        errors.append(
                            f"{label} line {i}: non-ASCII char U+{ord(ch):04X} "
                            f"in comment (possible encoding corruption)"
                        )
                        break

        # Check for duplicate ].join( blocks (sign of merge artifact)
        join_count = content.count("].join(")
        if join_count > 1:
            errors.append(
                f"{label}: found {join_count} '].join(' occurrences "
                f"(expected 1 — possible duplicate CSS block)"
            )

        # Syntax-check Python modules
        if module_path.endswith(".py"):
            try:
                compile(content, module_path, "exec")
            except SyntaxError as e:
                errors.append(f"{label}: syntax error at line {e.lineno}: {e.msg}")

    if errors:
        print("\n=== Pre-bundle validation FAILED ===", file=sys.stderr)
        for err in errors:
            print(f"  ERROR: {err}", file=sys.stderr)
        print(file=sys.stderr)
        sys.exit(1)

    print("Pre-bundle validation passed.")


def bundle():
    """Bundle the modular source into a single deployable file."""
    validate_sources()

    # Validate source file exists
    if not os.path.isfile(SOURCE_FILE):
        print(f"Error: Source tool not found at {SOURCE_FILE}", file=sys.stderr)
        sys.exit(1)

    bundled = read_file(SOURCE_FILE)

    # Inline each module
    for module_path, label, block_start, block_end in MODULES:
        module_source = read_file(module_path)
        module_body = extract_module_body(module_source)

        start_idx = bundled.find(block_start)
        end_idx = bundled.find(block_end)

        if start_idx == -1 or end_idx == -1:
            print(
                f"Error: Could not find {label} markers in source.\n"
                f"Expected '{block_start}' and '{block_end}'.",
                file=sys.stderr,
            )
            sys.exit(1)

        # Include the end marker line itself
        end_idx = bundled.index("\n", end_idx) + 1

        # Build the inlined block
        inlined_block = (
            f"{block_start}\n"
            f"{module_body}\n"
            f"{block_end}\n"
        )

        # Replace the import block with inlined code
        bundled = bundled[:start_idx] + inlined_block + bundled[end_idx:]

    return bundled


def main():
    parser = argparse.ArgumentParser(
        description="Bundle SynapseFM tool into a single file for Open WebUI."
    )
    parser.add_argument(
        "--output", "-o",
        default=DEFAULT_OUTPUT,
        help=f"Output filename (placed in dist/). Default: {DEFAULT_OUTPUT}",
    )
    args = parser.parse_args()

    bundled = bundle()

    # Write to dist/
    os.makedirs(DIST_DIR, exist_ok=True)
    output_path = os.path.join(DIST_DIR, args.output)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(bundled)

    # Calculate stats
    line_count = bundled.count("\n") + 1
    byte_count = len(bundled.encode("utf-8"))

    print(f"Bundled successfully: {output_path}")
    print(f"  Lines: {line_count}")
    print(f"  Size:  {byte_count:,} bytes ({byte_count / 1024:.1f} KB)")
    print(f"\nPaste the contents of {output_path} into Open WebUI -> Workspace -> Tools.")


if __name__ == "__main__":
    main()
