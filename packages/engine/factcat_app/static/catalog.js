/** Cached warehouse lists: paint immediately, fetch if empty, refresh on demand.

Two catalogs share this: event names (lookback DISTINCT, or fc_event_names
when a write dest is set) and table columns (metadata). Dropdowns that
read the same catalog share one refresh.
*/
function bindCachedList(options) {
  const loaders = [].concat(options.loadingFor || []).filter(Boolean);
  const buttons = [].concat(options.refreshButtons || []).filter(Boolean);
  const statusEl = options.statusFor || null;
  let items = Array.isArray(options.items) ? options.items.slice() : [];
  let gen = 0;
  options.paint(items);

  function setBusy(on, user) {
    loaders.forEach((el) => {
      const hint = document.getElementById(el.id + "-loading");
      if (hint) hint.hidden = !on;
    });
    // Class, not `hidden`: other chrome (write-dest, split chevron) owns hidden.
    buttons.forEach((btn) => btn.classList.toggle("refresh-busy", on));
    if (statusEl) {
      statusEl.hidden = !on;
      statusEl.textContent = on && options.statusText ? options.statusText(!!user) : "";
    }
  }

  async function refresh(user) {
    const mine = ++gen;
    setBusy(true, user);
    try {
      const next = await options.load({ user: !!user });
      if (mine !== gen) return items;
      if (next == null) return items;
      items = Array.isArray(next) ? next.slice() : [];
      options.paint(items);
      return items;
    } finally {
      if (mine === gen) setBusy(false, user);
    }
  }

  buttons.forEach((btn) => btn.addEventListener("click", () => { refresh(true); }));
  const start = typeof options.loadOnStart === "function"
    ? options.loadOnStart()
    : options.loadOnStart;
  if (start || (!items.length && options.loadIfEmpty)) refresh(false);
  return { refresh, items: () => items };
}
