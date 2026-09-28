(function (root) {
  async function responseJson(response, fallback = 'Не удалось выполнить запрос.') {
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : fallback);
    return data;
  }
  function analyticsAfterFailure() { return null; }
  function shouldReleaseBusy(requestId, currentId) { return requestId === currentId; }
  root.RetroState = {responseJson, analyticsAfterFailure, shouldReleaseBusy};
})(globalThis);
