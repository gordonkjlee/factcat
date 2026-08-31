/** Cached warehouse lists: paint immediately, refresh on demand.

Two catalogs share this: event names (DISTINCT query) and table columns
(metadata). Dropdowns that read the same catalog share one refresh.
*/
function bindCachedList(options) {
  const loaders = [].concat(options.loadingFor || []).filter(Boolean);
  const buttons = [].concat(options.refreshButtons || []).filter(Boolean);
  let items = Array.isArray(options.items) ? options.items.slice() : [];
  let gen = 0;
  options.paint(items);

  function setBusy(on) {
    loaders.forEach((el) => {
      const hint = document.getElementById(el.id + "-loading");
      if (hint) hint.hidden = !on;
    });
  }

  async function refresh() {
    const mine = ++gen;
    setBusy(true);
    try {
      const next = await options.load();
      if (mine !== gen) return items;
      if (next == null) return items;
      items = Array.isArray(next) ? next.slice() : [];
      options.paint(items);
      return items;
    } finally {
      if (mine === gen) setBusy(false);
    }
  }

  buttons.forEach((btn) => btn.addEventListener("click", () => { refresh(); }));
  if (!items.length && options.loadIfEmpty) refresh();
  return { refresh, items: () => items };
}
