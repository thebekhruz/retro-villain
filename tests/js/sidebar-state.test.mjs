import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile, readdir} from 'node:fs/promises';

// Меню справа (T-502). sidebar-state.js читается до первого кадра и ставит
// класс на <html>; в node нет ни документа, ни хранилища — подставляем свои.
const STATIC = new URL('../../retro/static/', import.meta.url);

function classes() {
  const names = new Set();
  return {
    add: name => names.add(name),
    remove: name => names.delete(name),
    contains: name => names.has(name),
    toggle(name, force) {
      const on = force === undefined ? !names.has(name) : force;
      if (on) names.add(name); else names.delete(name);
      return on;
    },
  };
}

function memory(initial = {}) {
  const data = new Map(Object.entries(initial));
  return {data, getItem: key => (data.has(key) ? data.get(key) : null), setItem: (key, value) => data.set(key, String(value))};
}

function useStorage(storage) {
  Object.defineProperty(globalThis, 'localStorage', {value: storage, configurable: true, writable: true});
}

// Приватный режим Safari и запрет данных сайта: падает сам доступ к хранилищу.
function blockStorage() {
  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    get() { throw new Error('SecurityError'); },
  });
}

function page() {
  const root = {classList: classes()};
  globalThis.document = {readyState: 'loading', addEventListener() {}, documentElement: root};
  return root;
}

let root = page();
useStorage(memory({'retro:sidebar-side': 'right', 'retro:sidebar': 'collapsed'}));
await import('../../retro/static/sidebar-state.js');
const side = globalThis.RetroSidebar;

test('сохранённое «справа» ставит класс ещё при чтении скрипта, до первого кадра', () => {
  assert.equal(root.classList.contains('sidebar-right'), true);
  assert.equal(side.current(), 'right');
  // Свёрнутое меню (T-442) читается как раньше и не мешает положению.
  assert.equal(root.classList.contains('sidebar-collapsed'), true);
});

test('по умолчанию меню слева: пустое хранилище и чужие значения', () => {
  for (const value of [undefined, 'left', 'RIGHT', 'да', '']) {
    useStorage(memory(value === undefined ? {} : {'retro:sidebar-side': value}));
    assert.equal(side.saved(), 'left', String(value));
  }
  assert.equal(side.apply(side.saved()), 'left');
  assert.equal(root.classList.contains('sidebar-right'), false);
});

test('выбор запоминается на устройстве и снимается обратно', () => {
  const storage = memory();
  useStorage(storage);
  assert.equal(side.choose('right'), 'right');
  assert.equal(root.classList.contains('sidebar-right'), true);
  assert.equal(storage.data.get('retro:sidebar-side'), 'right');
  assert.equal(side.saved(), 'right');

  assert.equal(side.choose('left'), 'left');
  assert.equal(root.classList.contains('sidebar-right'), false);
  assert.equal(storage.data.get('retro:sidebar-side'), 'left');
  assert.equal(side.current(), 'left');
});

test('неизвестное положение — слева', () => {
  useStorage(memory());
  assert.equal(side.choose('top'), 'left');
  assert.equal(root.classList.contains('sidebar-right'), false);
});

test('без доступа к хранилищу меню всё равно переезжает, только не запоминается', () => {
  blockStorage();
  assert.equal(side.saved(), 'left');
  assert.equal(side.choose('right'), 'right');
  assert.equal(root.classList.contains('sidebar-right'), true);
  side.choose('left');
});

test('закрытое хранилище при загрузке не роняет страницу: меню слева', async () => {
  root = page();
  blockStorage();
  await import('../../retro/static/sidebar-state.js?storage-blocked');
  assert.equal(root.classList.contains('sidebar-right'), false);
  assert.equal(root.classList.contains('sidebar-collapsed'), false);
  assert.equal(globalThis.RetroSidebar.current(), 'left');
});

test('скрипт положения подключён без defer в <head> каждой страницы с меню', async () => {
  const pages = (await readdir(STATIC)).filter(name => name.endsWith('.html'));
  let checked = 0;
  for (const name of pages) {
    const html = await readFile(new URL(name, STATIC), 'utf8');
    if (!html.includes('data-collapsible')) continue;
    checked += 1;
    const tag = html.match(/<script[^>]*src="\/static\/sidebar-state\.js"[^>]*>/);
    assert.ok(tag, name);
    assert.doesNotMatch(tag[0], /\b(defer|async)\b/, name);
    assert.ok(html.indexOf(tag[0]) < html.indexOf('<body'), name);
  }
  assert.ok(checked >= 10, `страниц с меню: ${checked}`);
});

/* Правила меню справа — только от 651 px: на телефоне меню — полоса сверху,
   и там ничего не должно меняться. Скобки разбираем вручную: для каждого
   селектора с классом собираем @media, внутри которых он стоит. */
function rulesWith(css, needle) {
  const found = [];
  const stack = [];
  let prelude = '';
  for (const char of css.replace(/\/\*[\s\S]*?\*\//g, '')) {
    if (char === '{') {
      const head = prelude.trim();
      if (head.includes(needle)) found.push({selector: head, media: stack.filter(item => item.startsWith('@media'))});
      stack.push(head);
      prelude = '';
    } else if (char === '}') {
      stack.pop();
      prelude = '';
    } else if (char === ';') {
      prelude = '';
    } else {
      prelude += char;
    }
  }
  return found;
}

test('меню справа меняет раскладку только от 651 px', async () => {
  const css = await readFile(new URL('style.css', STATIC), 'utf8');
  const rules = rulesWith(css, 'sidebar-right');
  assert.ok(rules.length >= 8, `правил: ${rules.length}`);
  for (const rule of rules) {
    assert.ok(rule.media.some(media => /min-width:\s*(651|901)px/.test(media)), rule.selector);
  }
  // Сама кнопка на телефоне не показывается.
  const hidden = rulesWith(css, '.side-place').filter(rule => rule.media.some(media => /max-width:\s*650px/.test(media)));
  assert.ok(hidden.length, 'нет правила, прячущего кнопку на телефоне');
});

test('подпись переключателя переводится на узбекский', async () => {
  await import('../../retro/static/i18n-uz.js');
  await import('../../retro/static/i18n.js');
  assert.equal(globalThis.RetroI18n.translate('Меню справа'), "Menyu o'ngda");
});
