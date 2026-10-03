from html import escape


def render_admin_page(name: str, author: str) -> str:
    name, author = escape(name), escape(author)  # author names are user-chosen
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{name} · admin</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 32rem; margin: 4rem auto; padding: 0 1rem; color: #1a1a1a; }}
  dl {{ display: grid; grid-template-columns: max-content 1fr; gap: .5rem 1.5rem; }}
  dt {{ color: #666; }}
  dd {{ margin: 0; font-weight: 600; }}
  a {{ color: inherit; }}
</style>
</head>
<body>
<h1>App details</h1>
<dl>
  <dt>Name</dt><dd>{name}</dd>
  <dt>Author</dt><dd>{author}</dd>
</dl>
<p><a href="../">← Back to app</a></p>
</body>
</html>
"""
