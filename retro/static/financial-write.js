/* Keep a request key across an ambiguous failure and a page reload in this tab. */
window.RetroFinancialWrite = async function(url, options) {
  const storageKey = 'retro-financial-write:' + url + ':' + options.body;
  let key = sessionStorage.getItem(storageKey);
  if (!key) { key = crypto.randomUUID(); sessionStorage.setItem(storageKey, key); }
  const response = await fetch(url, {...options, headers: {...options.headers, 'Idempotency-Key': key}});
  await response.clone().arrayBuffer();
  if (response.ok || (response.status >= 400 && response.status < 500 && response.status !== 409)) {
    sessionStorage.removeItem(storageKey);
  }
  return response;
};
