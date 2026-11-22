"""Build the HTML viewers for the installation guides from their Markdown sources and template.html."""
import html, json, pathlib
here = pathlib.Path(__file__).parent
GUIDES = [("guide-1-single-user", "🏠 Guide 1 · Single user", "Guide 1 — Single user"),
          ("guide-2-dedicated-factory", "🏭 Guide 2 · Dedicated factory", "Guide 2 — Dedicated factory"),
          ("guide-3-operating-the-factory", "🎛️ Guide 3 · Operating the factory", "Guide 3 — Operating the factory")]
tpl = (here / "template.html").read_text()
for name, label, title in GUIDES:
    nav = "<nav><b>Synth City guides</b>" + "".join(
        f'<a href="{n}.html"{" class=\"on\"" if n == name else ""}>{l}</a>' for n, l, _ in GUIDES
    ) + '<span id="mstat" style="margin-left:auto;font-size:13px;color:#aab3c0"></span></nav>'
    md = (here / f"{name}.md").read_text()
    out = (tpl.replace("__TITLE__", html.escape(title)).replace("__NAV__", nav)
           .replace("__MD__", json.dumps(md).replace("</", "<\\/")))
    (here / f"{name}.html").write_text(out)
    print(name + ".html", len(out))
