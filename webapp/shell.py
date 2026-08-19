"""One shared UI shell for every page.

Everything the app renders -- the tool, the dashboard, the admin console, and
the public marketing pages -- comes through here, so there is a single place
that owns the logo, the sidebar, the stylesheet and the chrome. Previously each
page carried its own copy and they drifted into looking like different products.
"""

MARK = ('<svg viewBox="0 0 24 24" fill="none"><path d="M4 12.5l5.5 5.5L20 6.5"'
        ' stroke="white" stroke-width="3" stroke-linecap="round"'
        ' stroke-linejoin="round"/></svg>')

_ICON = {
    "single": '<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><path d="M22 4L12 14.01l-3-3"/>',
    "bulk": '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5-5 5 5"/><path d="M12 5v13"/>',
    "find": '<circle cx="11" cy="11" r="7"/><path d="M21 21l-4.35-4.35"/>',
    "history": '<path d="M3 3v5h5"/><path d="M3.05 13A9 9 0 1 0 6 5.3L3 8"/><path d="M12 7v5l4 2"/>',
    "key": '<path d="M21 2l-2 2m-7.61 7.61a5.5 5.5 0 1 1-7.778 7.778 5.5 5.5 0 0 1 7.777-7.777zm0 0L15.5 7.5m0 0l3 3L22 7l-3-3m-3.5 3.5L19 4"/>',
    "docs": '<path d="M16 18l6-6-6-6"/><path d="M8 6l-6 6 6 6"/>',
    "plans": '<rect x="2" y="5" width="20" height="14" rx="2"/><path d="M2 10h20"/>',
    "admin": '<path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>',
    "logout": '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="M16 17l5-5-5-5"/><path d="M21 12H9"/>',
}


def icon(name: str) -> str:
    return ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"'
            ' stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">'
            '%s</svg>' % _ICON.get(name, ""))


def _item(href: str, name: str, label: str, active: bool) -> str:
    return ('<a class="nav-item%s" href="%s">%s%s</a>'
            % (" active" if active else "", href, icon(name), label))


def sidebar(account: dict, active: str = "") -> str:
    """The one sidebar. `active` is a view key or a page key like 'dashboard'."""
    tool = ""
    for key, label in (("single", "Single check"), ("bulk", "Bulk verification"),
                       ("find", "Find email"), ("history", "History")):
        # Tool views are switched client-side on /app; from other pages we link back.
        tool += ('<a class="nav-item%s" href="/app#%s" data-view="%s">%s%s</a>'
                 % (" active" if active == key else "", key, key, icon(key), label))

    admin_item = _item("/admin", "admin", "Admin", active == "admin") \
        if account.get("is_admin") else ""

    used = account.get("_used", 0)
    quota = account.get("daily_quota", 0) or 0
    pct = min(100, (used / quota * 100)) if quota else 0

    return """
<aside class="sidebar" id="sidebar">
  <a class="side-brand" href="/app"><span class="logo">{mark}</span>Xomexo</a>

  <div class="quota-box">
    <div class="lbl">Daily quota</div>
    <div class="val"><b id="qUsed">{used}</b><span id="qTotal">/ {quota}</span></div>
    <div class="quota-bar"><i id="qBar" style="width:{pct:.0f}%"></i></div>
  </div>

  <nav class="side-nav">
    <div class="nav-group">Email verification</div>
    {tool}
    <div class="nav-group">Developer</div>
    {key}
    {docs}
    <div class="nav-group">Account</div>
    {plans}
    {admin}
    {logout}
  </nav>

  <div class="side-foot">
    <button id="themeToggle" class="ghost" style="width:100%;justify-content:center">
      <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor"
        stroke-width="1.8"><circle cx="12" cy="12" r="9"/>
        <path d="M12 3a9 9 0 0 0 0 18z" fill="currentColor" stroke="none"/></svg>
      Theme
    </button>
  </div>
</aside>""".format(
        mark=MARK, used="{:,}".format(used), quota="{:,}".format(quota), pct=pct,
        tool=tool,
        key=_item("/dashboard", "key", "API key &amp; usage", active == "dashboard"),
        docs=_item("/docs-api", "docs", "API docs", active == "docs"),
        plans=_item("/pricing", "plans", "Plans", active == "pricing"),
        admin=admin_item,
        logout=_item("/account/logout", "logout", "Log out", False))


def page(title: str, account: dict, body: str, active: str = "",
         wide: bool = False) -> str:
    """A full signed-in page: shared <head>, sidebar, header, and body."""
    return """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — Xomexo</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><rect width='100' height='100' rx='24' fill='%231f6feb'/><path d='M28 52l16 16 30-32' stroke='white' stroke-width='10' fill='none' stroke-linecap='round' stroke-linejoin='round'/></svg>">
<link rel="stylesheet" href="/static/app.css">
</head><body>
<div class="shell">
{side}
  <div class="app-main">
    <header class="app-head">
      <button class="ghost side-toggle" id="sideToggle" title="Menu">
        <svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor"
          stroke-width="2" stroke-linecap="round"><path d="M3 6h18M3 12h18M3 18h18"/></svg>
      </button>
      <h1>{title}</h1><div class="spacer"></div>
      <span class="muted">{email}</span>
    </header>
    <div class="app-body"{style}>
{body}
    </div>
  </div>
</div>
<script>
  var t = localStorage.getItem("theme");
  if (t) document.documentElement.dataset.theme = t;
  var tt = document.getElementById("themeToggle");
  if (tt) tt.onclick = function () {{
    var n = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = n; localStorage.setItem("theme", n);
  }};
  var st = document.getElementById("sideToggle");
  if (st) st.onclick = function () {{
    document.getElementById("sidebar").classList.toggle("open");
  }};
</script>
</body></html>""".format(
        title=title, side=sidebar(account, active), email=account.get("email", ""),
        body=body, style=' style="max-width:1240px"' if wide else "")


def public_page(title: str, body: str, active: str = "") -> str:
    """Marketing/auth pages: same tokens and logo, simple centred header."""
    def link(href, label, key):
        return '<a href="%s"%s>%s</a>' % (
            href, ' class="on"' if active == key else "", label)
    return """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — Xomexo</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><rect width='100' height='100' rx='24' fill='%231f6feb'/><path d='M28 52l16 16 30-32' stroke='white' stroke-width='10' fill='none' stroke-linecap='round' stroke-linejoin='round'/></svg>">
<link rel="stylesheet" href="/static/app.css">
<link rel="stylesheet" href="/static/public.css">
</head><body class="pub">
<header class="pub-nav">
  <a class="pub-brand" href="/"><span class="logo">{mark}</span>Xomexo</a>
  <nav class="pub-links">{home}{pricing}{docs}</nav>
  <div class="pub-cta"><a href="/account" class="btn">Sign in</a></div>
</header>
<main class="pub-main">
{body}
</main>
<footer class="pub-foot">
  <span>&copy; Xomexo</span>
  <a href="/pricing">Pricing</a><a href="/docs-api">API docs</a><a href="/account">Sign in</a>
</footer>
<script>
  var t = localStorage.getItem("theme"); if (t) document.documentElement.dataset.theme = t;
</script>
</body></html>""".format(
        title=title, mark=MARK, body=body,
        home=link("/", "Home", "home"),
        pricing=link("/pricing", "Pricing", "pricing"),
        docs=link("/docs-api", "API docs", "docs"))
