import os
import re


def fix_file(filepath):
    with open(filepath, encoding="utf-8") as f:
        content = f.read()

    # Regex to find templates.TemplateResponse calls
    # We want to replace templates.TemplateResponse("..." with templates.TemplateResponse(request, "..."
    # but only if it doesn't already start with templates.TemplateResponse(request

    # We can match templates.TemplateResponse( and check if the next word is 'request'
    # if it is not, we insert 'request, '

    modified = False

    # Find all occurrences of templates.TemplateResponse(
    pattern = re.compile(r"templates\.TemplateResponse\(")

    new_content = []
    last_idx = 0

    for match in pattern.finditer(content):
        end_idx = match.end()

        # Check what follows templates.TemplateResponse(
        # skip whitespace
        check_idx = end_idx
        while check_idx < len(content) and content[check_idx].isspace():
            check_idx += 1

        # If it doesn't start with 'request', insert 'request, '
        if not content[check_idx:].startswith("request"):
            new_content.append(content[last_idx:end_idx])
            new_content.append("request, ")
            last_idx = end_idx
            modified = True

    if modified:
        new_content.append(content[last_idx:])
        final_content = "".join(new_content)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(final_content)
        print(f"Fixed {filepath}")
    else:
        print(f"No changes needed for {filepath}")


def main():
    paths = [
        "app/main.py",
    ]

    routes_dir = "app/routes"
    for filename in os.listdir(routes_dir):
        if filename.endswith(".py"):
            paths.append(os.path.join(routes_dir, filename))

    for path in paths:
        if os.path.exists(path):
            fix_file(path)


if __name__ == "__main__":
    main()
